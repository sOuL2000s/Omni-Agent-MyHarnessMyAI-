@echo off
cd /d %~dp0
python -m venv .venv
call .venv\Scripts\activate
pip install -r requirements.txt
if not exist .env copy .env.example .env
echo.
echo Done. Start the web UI with:  omni-ui.bat     (terminal chat: omni.bat)
echo Add your API keys in the UI under "Keys & models", or edit .env.
