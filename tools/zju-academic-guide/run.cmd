@echo off
REM ============================================================
REM  Step 2 - start the guide. Double-click this file to run.
REM  If it says the environment is missing, run setup.cmd first.
REM
REM  NOTE: this file must contain NO non-ASCII characters. cmd.exe
REM  reads .cmd files in the OEM codepage, so a UTF-8 Chinese path
REM  written here would be corrupted. The program folder name is
REM  resolved by wildcard matching instead.
REM ============================================================
setlocal

set "HERE=%~dp0"
cd /d "%HERE%"

set "VPY=%HERE%.venv\Scripts\python.exe"

REM resolve the program folder without hardcoding a non-ASCII name
set "SRC="
for /d %%D in ("%HERE%*") do (
  if exist "%%~fD\academic_guide.py" set "SRC=%%~fD"
)

echo ============================================================
echo   ZJU academic guide
echo ============================================================
echo.

if not exist "%VPY%" (
  echo [ERROR] Environment not set up yet.
  echo Please double-click  setup.cmd  first - one time only.
  goto :end
)

if not defined SRC (
  echo [ERROR] could not find academic_guide.py in a subfolder of:
  echo   %HERE%
  goto :end
)

echo Using program folder:
echo   %SRC%
echo.

REM keep temp and browser files inside this folder
if not exist "%HERE%var" mkdir "%HERE%var"
if not exist "%HERE%var\tmp" mkdir "%HERE%var\tmp"
set "TMP=%HERE%var\tmp"
set "TEMP=%HERE%var\tmp"
set "PLAYWRIGHT_BROWSERS_PATH=%HERE%var\playwright-browsers"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

echo. >> "%HERE%var\guide-run.log"
echo ==== run start %DATE% %TIME% ==== >> "%HERE%var\guide-run.log"

REM kill leftover browser processes from a previous crashed run
"%VPY%" "%SRC%\kill_stale_browser.py" >> "%HERE%var\guide-run.log" 2>&1

"%VPY%" "%SRC%\academic_guide.py" %*
set "RC=%ERRORLEVEL%"

echo.
echo ------------------------------------------------------------
if "%RC%"=="0" (
  echo [DONE] Exit code 0
) else (
  echo [FAILED] Exit code %RC%
  echo Run log: "%HERE%var\guide-run.log"
  if exist "%HERE%var\guide-error.log" echo Error  : "%HERE%var\guide-error.log"
)
echo Data dir: "%HERE%var\academics"
echo ------------------------------------------------------------
echo.

:end
pause
endlocal
