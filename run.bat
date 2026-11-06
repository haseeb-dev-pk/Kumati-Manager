@echo off
cd /d "%~dp0"
if not exist "venv" (
  python -m venv venv
  call "venv\Scripts\activate"
  python -m pip install -r requirements.txt
) else (
  call "venv\Scripts\activate"
)
python app.py
pause
