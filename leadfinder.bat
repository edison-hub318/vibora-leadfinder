@echo off
REM Runs leadfinder using the project's virtual environment.
REM Double-click to open the dashboard, or call from a terminal:  leadfinder.bat list --top 10
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\activate.bat" (
  echo The virtual environment is missing. Follow the setup steps in README.md first:
  echo   py -m venv .venv
  echo   .venv\Scripts\Activate.ps1
  echo   pip install -e .
  pause
  exit /b 1
)
call ".venv\Scripts\activate.bat"
if "%~1"=="" (
  leadfinder ui
) else (
  leadfinder %*
)
set EXITCODE=%ERRORLEVEL%
REM Keep the window open when double-clicked so you can read any messages.
echo %cmdcmdline% | find /i "/c" >nul && pause
endlocal & exit /b %EXITCODE%
