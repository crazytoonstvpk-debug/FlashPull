@echo off
title FlashPull - Build EXE
where python >nul 2>nul
if errorlevel 1 (
  echo Python install nahi hai. Download page khul raha hai...
  echo Install karte waqt "Add Python to PATH" ZAROOR tick karna.
  start https://www.python.org/downloads/
  pause
  exit /b
)
python -m pip install --upgrade pyinstaller
python -m PyInstaller --onefile --noconsole --name FlashPull flashpull.py
echo.
echo Done! Aapka software yahan hai: dist\FlashPull.exe
explorer dist
pause
