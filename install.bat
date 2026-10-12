@echo off
cd /d %~dp0
python -m venv .venv
call .venv\Scripts\activate
pip install -r requirements.txt
if not exist .env copy .env.example .env
echo Done. Install OmniRoute with: npm install -g omniroute   then edit .env and run omni.bat check
