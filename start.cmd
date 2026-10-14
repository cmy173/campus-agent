@echo off
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
    py -3 launch.py %*
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo Install Python 3.12 from https://www.python.org/downloads/ and enable Add Python to PATH.
        pause
        exit /b 1
    )
    python launch.py %*
)
if errorlevel 1 (
    pause
    exit /b 1
)
