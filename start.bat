@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python not found. Please install Python 3.10+ and retry.
  pause
  exit /b 1
)
python app.py %*
set "INTERVIEW_EXIT=%ERRORLEVEL%"
if not "%INTERVIEW_EXIT%"=="0" (
  echo.
  echo Startup failed. See the error above, then press any key to close.
  pause >nul
)
exit /b %INTERVIEW_EXIT%
