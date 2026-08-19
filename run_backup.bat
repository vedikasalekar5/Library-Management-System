@echo off

cd /d "%~dp0"

if not exist "backups" (
    mkdir "backups"
)

python "backup_database.py" >> "backups\task_scheduler_output.txt" 2>&1