@echo off
cd /d "%%~dp0"
echo Resetting LiveLens onboarding...
if exist "venv\Scripts\python.exe" (
    "venv\Scripts\python.exe" -c "from utils.first_run import FirstRunStore; FirstRunStore().set_should_show_guide(True)"
    if errorlevel 1 pause & exit /b 1
    "venv\Scripts\python.exe" main.py
) else (
    python -c "from utils.first_run import FirstRunStore; FirstRunStore().set_should_show_guide(True)"
    if errorlevel 1 pause & exit /b 1
    python main.py
)
if errorlevel 1 pause
