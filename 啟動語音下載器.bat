@echo off
powershell -WindowStyle Hidden -Command "& { $d = Split-Path '%~f0'; Start-Process (Join-Path $d '..\python_embed\pythonw.exe') -ArgumentList (Join-Path $d 'launcher.pyw') -WindowStyle Hidden }"
