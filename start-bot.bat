@echo off
chcp 65001 >nul
setlocal

cd /d "%~dp0"

set "VENV=.venv"
set "PYTHON_EXE=%VENV%\Scripts\python.exe"
set "PY_CMD=py -3.12"

echo [1/4] Checking Python...
%PY_CMD% --version >nul 2>&1
if errorlevel 1 (
    set "PY_CMD=python"
    python --version >nul 2>&1
    if errorlevel 1 (
        set "PY_CMD=py -3"
        py -3 --version >nul 2>&1
        if errorlevel 1 goto python_failed
    )
)

for /f "tokens=2 delims= " %%V in ('%PY_CMD% --version 2^>^&1') do set "PY_VERSION=%%V"
echo Using Python %PY_VERSION%
echo %PY_VERSION% | findstr /r "^3\.14" >nul
if not errorlevel 1 (
    echo Python 3.14 is not recommended for faster-whisper on Windows.
    echo Install Python 3.12 or use Docker/Dokploy for voice recognition.
    goto failed
)

if not exist "%PYTHON_EXE%" (
    echo Creating virtual environment in %VENV%...
    %PY_CMD% -m venv "%VENV%"
    if errorlevel 1 goto python_failed
)

"%PYTHON_EXE%" --version >nul 2>&1
if errorlevel 1 (
    echo Existing virtual environment is broken. Recreating %VENV%...
    rmdir /s /q "%VENV%"
    %PY_CMD% -m venv "%VENV%"
    if errorlevel 1 goto python_failed
)

for /f "tokens=2 delims= " %%V in ('"%PYTHON_EXE%" --version 2^>^&1') do set "VENV_VERSION=%%V"
echo Existing venv Python %VENV_VERSION%
echo %VENV_VERSION% | findstr /r "^3\.14" >nul
if not errorlevel 1 (
    echo Recreating %VENV% because Python 3.14 is not recommended for faster-whisper.
    rmdir /s /q "%VENV%"
    %PY_CMD% -m venv "%VENV%"
    if errorlevel 1 goto python_failed
)

echo [2/4] Installing dependencies...
"%PYTHON_EXE%" -m pip install --upgrade pip
if errorlevel 1 goto pip_failed

"%PYTHON_EXE%" -m pip install -r requirements.txt
if errorlevel 1 goto pip_failed

echo [3/4] Checking .env...
if not exist ".env" (
    echo File .env was not found.
    echo Create it from .env.example and set BOT_TOKEN.
    goto failed
)

echo [4/4] Starting Telegram bot...
"%PYTHON_EXE%" -m src.main
goto done

:python_failed
echo Could not create virtual environment.
echo Install Python 3.12+ and make sure the python command is available.
goto failed

:pip_failed
echo Could not install dependencies.
echo Check your internet connection and try again.
goto failed

:failed
echo.
echo Bot was not started.
pause
exit /b 1

:done
echo.
echo Bot process finished.
pause
