@echo off
cd /d %~dp0
where python >nul 2>nul
if %errorlevel%==0 (
    python app.py
    goto end
)
where python3 >nul 2>nul
if %errorlevel%==0 (
    python3 app.py
    goto end
)
echo Python not found. Install Python 3, or use the exe version.
:end
pause
