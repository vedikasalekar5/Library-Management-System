@echo off
cd /d "%~dp0"
python send_whatsapp_reminders.py
if errorlevel 1 pause
