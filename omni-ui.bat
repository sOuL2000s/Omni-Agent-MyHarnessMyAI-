@echo off
cd /d %~dp0
if not exist .venv\Scripts\activate.bat ( echo First run: setting up... & call install.bat )
call .venv\Scripts\activate
python -m agent ui %*
