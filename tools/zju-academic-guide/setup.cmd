@echo off
REM ============================================================
REM  Step 1 - one-time setup. Double-click this file first.
REM  Finds a real Python, creates a local virtualenv, installs
REM  playwright + pypdf, and sets up the browser driver.
REM  ASCII only: cmd.exe reads .cmd files in the OEM codepage.
REM ============================================================
setlocal enabledelayedexpansion

set "HERE=%~dp0"
cd /d "%HERE%"

echo ============================================================
echo   ZJU academic guide - first-time setup
echo ============================================================
echo.
echo This folder:
echo   %HERE%
echo.

REM ------------------------------------------------------------
REM  1. Find a WORKING python.
REM     Note: a "python" that only exists under WindowsApps is the
REM     Microsoft Store stub - it is NOT a real interpreter and
REM     exits with 9009. We verify each candidate by running it.
REM ------------------------------------------------------------
set "PY="

REM candidate A: the Python bundled with DSH Desktop (present on this PC)
set "DSHPY=C:\Users\zhy20\AppData\Local\Programs\DSH Desktop\resources\office-runtime\primary-runtime\dependencies\python\python.exe"
if exist "%DSHPY%" (
  "%DSHPY%" -c "pass" >nul 2>nul
  if not errorlevel 1 (
    set "PY=%DSHPY%"
    echo [OK] found bundled Python
  )
)

REM candidate B: standard per-user / machine installs
if not defined PY (
  for %%D in ("%LOCALAPPDATA%\Programs\Python") do (
    if exist "%%~D" (
      for /d %%V in ("%%~D\Python3*") do (
        if exist "%%~V\python.exe" (
          "%%~V\python.exe" -c "pass" >nul 2>nul
          if not errorlevel 1 if not defined PY set "PY=%%~V\python.exe"
        )
      )
    )
  )
)

REM candidate C: whatever "where python" returns - but verify it actually runs
if not defined PY (
  for /f "delims=" %%P in ('where python 2^>nul') do (
    if not defined PY (
      echo %%P | find /i "WindowsApps" >nul
      if errorlevel 1 (
        "%%P" -c "pass" >nul 2>nul
        if not errorlevel 1 set "PY=%%P"
      )
    )
  )
)

REM candidate D: the py launcher - verify the interpreter it points at
if not defined PY (
  for /f "delims=" %%P in ('py -c "import sys;print(sys.executable)" 2^>nul') do (
    if exist "%%P" (
      "%%P" -c "pass" >nul 2>nul
      if not errorlevel 1 set "PY=%%P"
    )
  )
)

if not defined PY (
  echo [ERROR] No working Python found on this PC.
  echo.
  echo The "python" command may be the Microsoft Store placeholder, which
  echo cannot create environments. Please install real Python:
  echo.
  echo     https://www.python.org/downloads/
  echo.
  echo On the installer screen, TICK "Add python.exe to PATH", then run
  echo this setup again.
  goto :end
)

for /f "delims=" %%v in ('"%PY%" --version 2^>^&1') do set "PYVER=%%v"
echo Using: %PY%
echo        %PYVER%
echo.

REM ------------------------------------------------------------
REM  2. Create the virtualenv inside this folder
REM ------------------------------------------------------------
if exist "%HERE%.venv\Scripts\python.exe" (
  echo [OK] virtualenv already exists - skipping creation.
) else (
  echo Creating virtualenv in "%HERE%.venv" ...
  "%PY%" -m venv "%HERE%.venv"
  if not exist "%HERE%.venv\Scripts\python.exe" (
    echo [ERROR] Failed to create the virtualenv.
    goto :end
  )
  echo [OK] virtualenv created.
)
echo.

set "VPY=%HERE%.venv\Scripts\python.exe"

REM ------------------------------------------------------------
REM  3. Install the two dependencies
REM ------------------------------------------------------------
echo Installing dependencies - needs internet, may take a few minutes...
"%VPY%" -m pip install --upgrade pip --quiet
"%VPY%" -m pip install playwright pypdf --quiet
if errorlevel 1 (
  echo.
  echo [WARN] pip reported an error. In China, try the mirror:
  echo   "%VPY%" -m pip install playwright pypdf -i https://pypi.tuna.tsinghua.edu.cn/simple
  echo.
) else (
  echo [OK] dependencies installed.
)
echo.

REM ------------------------------------------------------------
REM  4. Browser driver (reuses the Edge already on this PC)
REM ------------------------------------------------------------
echo Preparing browser support...
if not exist "%HERE%var\playwright-browsers" mkdir "%HERE%var\playwright-browsers"
set "PLAYWRIGHT_BROWSERS_PATH=%HERE%var\playwright-browsers"
"%VPY%" -m playwright install msedge
echo.

echo ============================================================
echo   Setup finished. Now double-click  run.cmd  to start.
echo ============================================================

:end
pause
endlocal
