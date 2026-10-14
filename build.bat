@echo off
cd /d %~dp0
if not exist .venv\Scripts\activate.bat ( echo Run install.bat first & exit /b 1 )
call .venv\Scripts\activate
python build.py %*