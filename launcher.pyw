import os
import sys
import time
import threading
import webbrowser

BASE = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE)
sys.path.insert(0, BASE)

def open_browser():
    time.sleep(3)
    webbrowser.open('http://127.0.0.1:5000')

threading.Thread(target=open_browser, daemon=True).start()

from app import app
app.run(port=5000, debug=False, use_reloader=False)
