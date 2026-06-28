import os
import sys
import uuid
import json
import asyncio
import re
import random
from flask import Flask, request, jsonify, send_file, render_template
from flask_cors import CORS
from google import genai
import edge_tts

# Windows: 強制使用 SelectorEventLoop，避免 edge-tts 在 Flask 執行緒中無法接收音訊
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

app = Flask(__name__)
CORS(app)

AUDIO_DIR = os.path.join(os.path.dirname(__file__), "audio_cache")
os.makedirs(AUDIO_DIR, exist_ok=True)
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")


def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


# ── 聲音映射 ──────────────────────────────────────────────
VOICES = {
    "A": {"female": "en-US-JennyNeural",  "male": "en-US-GuyNeural"},
    "B": {"female": "en-US-AriaNeural",   "male": "en-US-AndrewNeural"},
}

# ── 劍橋英檢等級 ─────────────────────────────────────────
LEVELS = {
    "starters": {
        "name": "Pre-A1 劍橋 Starters",
        "desc": (
            "Use ONLY the most basic 500-word vocabulary: colors, numbers 1-20, family members, "
            "common animals, food, toys, and body parts. Present simple tense ONLY. "
            "Maximum 6 words per sentence. Very simple, repetitive patterns."
        )
    },
    "movers": {
        "name": "A1 劍橋 Movers",
        "desc": (
            "Use A1-level vocabulary including school subjects, sports, clothes, and weather. "
            "Simple past tense (was/were, played) allowed. Maximum 8 words per sentence."
        )
    },
    "flyers": {
        "name": "A2 劍橋 Flyers",
        "desc": (
            "Use A2-level vocabulary. Allow present/past/future tenses, can/could, going to. "
            "Maximum 10 words per sentence. Include descriptive adjectives and simple adverbs."
        )
    },
    "a2key": {
        "name": "A2 劍橋 Key (KET)",
        "desc": (
            "Use A2 Key vocabulary list. Natural daily conversation style. "
            "Varied tenses. Include common phrasal verbs and expressions."
        )
    },
    "b1": {
        "name": "B1 劍橋 Preliminary (PET)",
        "desc": (
            "Use B1-level vocabulary. More complex sentences with present perfect, "
            "conditionals. Natural conversational expressions and idioms."
        )
    },
}

SPEAKER_NAMES = {
    "female": ["Emma", "Lily", "Amy", "Mia", "Sara"],
    "male":   ["Tom", "Ben", "Jack", "Mike", "Leo"],
}

STYLE_DESC = {
    "gentle":    "speaks softly and kindly, uses warm encouraging phrases",
    "clear":     "speaks slowly and clearly, enunciates every word precisely",
    "childlike": "speaks like a young child, uses simple short sentences and cute expressions",
    "lively":    "speaks energetically and enthusiastically, uses exclamations and upbeat words",
    "polite":    "speaks very politely, always uses please/thank you/excuse me",
    "humorous":  "uses light humor, fun wordplay, and makes the conversation cheerful",
}

GENERATE_PROMPT = """\
Create a natural English dialogue about "{topic}" at {level_name} level for Taiwan elementary school students.

Speaker A is a {gender_a} character named {name_a}. {name_a}'s speaking style: {style_desc_a}
Speaker B is a {gender_b} character named {name_b}. {name_b}'s speaking style: {style_desc_b}

Level requirements: {level_desc}

Dialogue rules:
- 8–12 exchanges between Speaker A and Speaker B
- Each line starts with "A:" or "B:"
- Reflect each speaker's individual speaking style clearly and consistently throughout
- Include greetings and polite expressions
- Do NOT include any stage directions, actions, or descriptions in parentheses such as (Giggles), (Smiling), (Laughs), etc. Only spoken words.

Then, identify any words or phrases in the dialogue that are ABOVE the {level_name} level \
and provide a short Traditional Chinese translation (2–4 characters only, no explanation).

Respond ONLY with valid JSON (no markdown, no code block):
{{
  "dialogue": "A: ...\\nB: ...\\nA: ...",
  "vocabulary": [
    {{"word": "example", "zh": "中文"}}
  ]
}}
If no vocabulary exceeds the level, use an empty array for vocabulary."""


def parse_generate_response(raw: str):
    """從 Gemini 回應中提取 JSON"""
    raw = raw.strip()
    # 移除 markdown code fences
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    return json.loads(raw)


def clamp(val, lo, hi):
    return max(lo, min(hi, val))


def make_rate_str(base_pct: int, emotion_pct: int) -> str:
    total = clamp(base_pct + emotion_pct, -80, 80)
    return f"+{total}%" if total >= 0 else f"{total}%"


def make_pitch_str(base_pct: int, emotion_hz: int) -> str:
    # base_pct (-50~+50) → 對應 -10~+10 Hz
    base_hz = round(base_pct * 0.2)
    total = clamp(base_hz + emotion_hz, -20, 20)
    return f"+{total}Hz" if total >= 0 else f"{total}Hz"


def analyze_emotion(text: str):
    """回傳 (rate_emotion_pct, pitch_emotion_hz)"""
    t = text.strip()
    if re.search(r"[!！]", t):
        return +15, +8
    if re.search(r"[?？]$", t):
        return +5, +6
    if re.search(r"\.{3}|…", t):
        return -15, -4
    if re.match(r"^(Wow|Oh|Ah|Really|Cool|Great|Awesome|Yes|Yeah|No way)", t, re.I):
        return +20, +10
    if re.match(r"^(Bye|Goodbye|See you|Good night|Take care)", t, re.I):
        return -15, -6
    return 0, 0


async def synthesize_line(text, voice, rate_str, pitch_str):
    communicate = edge_tts.Communicate(text, voice, rate=rate_str, pitch=pitch_str)
    audio = b""
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio += chunk["data"]
    return audio


async def build_audio(lines, voice_a, voice_b, base_rate):
    all_audio = b""
    for speaker, text in lines:
        voice = VOICES["A"][voice_a] if speaker == "A" else VOICES["B"][voice_b]
        e_rate, e_pitch = analyze_emotion(text)
        rate_str  = make_rate_str(base_rate, e_rate)
        pitch_str = make_pitch_str(0, e_pitch)
        all_audio += await synthesize_line(text, voice, rate_str, pitch_str)
    return all_audio


def strip_stage_directions(text: str) -> str:
    """移除括號內的動作描述，例如 (Giggles softly)、(Smiling)"""
    text = re.sub(r"\([^)]*\)", "", text)
    text = re.sub(r"\[[^\]]*\]", "", text)
    return text.strip()

def parse_dialogue(text):
    lines = []
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("A:"):
            content = strip_stage_directions(line[2:].strip())
            if content:
                lines.append(("A", content))
        elif line.startswith("B:"):
            content = strip_stage_directions(line[2:].strip())
            if content:
                lines.append(("B", content))
        elif line and lines:
            cleaned = strip_stage_directions(line)
            if cleaned:
                lines[-1] = (lines[-1][0], lines[-1][1] + " " + cleaned)
    return lines


# ── Routes ───────────────────────────────────────────────

@app.route("/")
def index():
    config = load_config()
    return render_template("index.html",
                           preset_api_key=config.get("google_api_key", ""),
                           levels=LEVELS)


@app.route("/save_key", methods=["POST"])
def save_key():
    key = request.json.get("api_key", "").strip()
    if not key:
        return jsonify({"error": "Key 不能為空"}), 400
    config = load_config()
    config["google_api_key"] = key
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    return jsonify({"ok": True})


@app.route("/generate", methods=["POST"])
def generate():
    data = request.json
    topic   = data.get("topic", "").strip()
    api_key = data.get("api_key", "").strip()
    level   = data.get("level", "movers")
    gender_a = data.get("voice_a", "female")   # "female" / "male"
    gender_b = data.get("voice_b", "male")
    styles_a = data.get("styles_a", [])
    styles_b = data.get("styles_b", [])

    if not topic:   return jsonify({"error": "請輸入主題"}), 400
    if not api_key: return jsonify({"error": "請輸入 API Key"}), 400

    name_a = random.choice(SPEAKER_NAMES[gender_a])
    name_b = random.choice(SPEAKER_NAMES[gender_b])
    while name_b == name_a:
        name_b = random.choice(SPEAKER_NAMES[gender_b])

    style_desc_a = "; ".join(STYLE_DESC[s] for s in styles_a if s in STYLE_DESC) or "speaks naturally and clearly"
    style_desc_b = "; ".join(STYLE_DESC[s] for s in styles_b if s in STYLE_DESC) or "speaks naturally and clearly"

    lv = LEVELS.get(level, LEVELS["movers"])
    prompt = GENERATE_PROMPT.format(
        topic=topic, level_name=lv["name"], level_desc=lv["desc"],
        gender_a=gender_a, name_a=name_a, style_desc_a=style_desc_a,
        gender_b=gender_b, name_b=name_b, style_desc_b=style_desc_b,
    )

    models = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-flash-latest"]
    client = genai.Client(api_key=api_key)
    for model in models:
        try:
            resp = client.models.generate_content(model=model, contents=prompt)
            parsed = parse_generate_response(resp.text)
            return jsonify({
                "dialogue":   parsed.get("dialogue", "").strip(),
                "vocabulary": parsed.get("vocabulary", []),
                "model": model,
                "level_name": lv["name"],
            })
        except json.JSONDecodeError:
            # Gemini 沒給合法 JSON，降級重試
            continue
        except Exception as e:
            err = str(e)
            if "429" in err or "quota" in err.lower():
                return jsonify({"error": "免費額度不足，請稍後再試。"}), 429
            if "503" in err or "UNAVAILABLE" in err:
                continue
            return jsonify({"error": err}), 500
    return jsonify({"error": "所有模型暫時過載，請稍後再試。"}), 503


@app.route("/synthesize", methods=["POST"])
def synthesize():
    data      = request.json
    dialogue  = data.get("dialogue", "").strip()
    voice_a   = data.get("voice_a", "female")
    voice_b   = data.get("voice_b", "male")
    base_rate = int(data.get("base_rate", 0))

    if not dialogue:
        return jsonify({"error": "請貼入對話內容"}), 400

    lines = parse_dialogue(dialogue)
    if not lines:
        lines = [("A", t) for t in dialogue.split("\n") if t.strip()]

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            audio = loop.run_until_complete(build_audio(
                lines, voice_a, voice_b, base_rate
            ))
        finally:
            loop.close()
            asyncio.set_event_loop(None)
        fname = f"{uuid.uuid4().hex}.mp3"
        fpath = os.path.join(AUDIO_DIR, fname)
        with open(fpath, "wb") as f:
            f.write(audio)
        return jsonify({"audio_id": fname})
    except Exception as e:
        return jsonify({"error": f"音檔生成失敗: {str(e)}"}), 500


@app.route("/download/<audio_id>")
def download(audio_id):
    if not all(c in "0123456789abcdefghijklmnopqrstuvwxyz._-" for c in audio_id):
        return "Invalid file", 400
    fpath = os.path.join(AUDIO_DIR, audio_id)
    if not os.path.exists(fpath):
        return "File not found", 404
    return send_file(fpath, as_attachment=True, download_name="english_dialogue.mp3")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    host = "0.0.0.0" if os.environ.get("RENDER") else "127.0.0.1"
    app.run(host=host, port=port, debug=False, use_reloader=False)
