@echo off
echo ============================================
echo   LinkedIn Automation - Setup (uv)
echo ============================================
echo.

REM Check uv
uv --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: uv not found. Install it:
    echo   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    pause
    exit /b 1
)

REM Create venv and install
echo Creating virtual environment and installing dependencies...
uv venv
uv pip install -r requirements.txt
REM Dev/test tooling (pytest, ruff) so 'uv run pytest' works after setup
uv pip install -r requirements-dev.txt

REM Create .env if missing
if not exist ".env" (
    echo.
    echo Creating .env from template...
    copy .env.example .env
    echo.
    echo *** IMPORTANT: Edit .env and add your OPENAI_API_KEY ***
    echo.
)

REM Create data directories
if not exist "data" mkdir data

echo.
echo ============================================
echo   Setup complete!
echo.
echo   Next steps:
echo   1. Edit .env with your OpenAI API key
echo   2. Add a profile: uv run python -m linkedin_automation.profile_manager add ^<name^>
echo   3. Log in once:   uv run python tools/login_check.py --profile ^<name^>
echo   4. Run: uv run python -m linkedin_automation.dashboard  (or double-click run.bat)
echo   5. Open: http://localhost:6500
echo ============================================
pause
