@echo off
rem Drishti-AI one-click launcher (source install). Double-click this file.
rem   * uses the first Python that has the app's packages: the .venv next to this file, "python", "py -3";
rem   * otherwise offers to install them into .venv (Python 3.10 or newer needed);
rem   * starts the app; the browser opens on the private link (keep this window open).
rem   run_app.bat --self-test   checks the installation and exits (used by CI).
setlocal EnableExtensions
title Drishti-AI - CCTV investigation
cd /d "%~dp0"
if exist "%~dp0packaging\ffmpeg\ffmpeg.exe" set "PATH=%~dp0packaging\ffmpeg;%PATH%"

set "PY="
for %%C in (".venv\Scripts\python.exe" "python" "py -3") do (
  if not defined PY ( %%~C -c "import cctv_analysis.app.server" >nul 2>nul && set "PY=%%~C" )
)
if defined PY goto :run

set "BASE="
for %%C in ("python" "py -3") do (
  if not defined BASE ( %%~C -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "BASE=%%~C" )
)
if not defined BASE (
  echo.
  echo  Python 3.10 or newer was not found on this computer.
  echo  1. Download Python 3.11 from https://www.python.org/downloads/windows/
  echo  2. In the installer tick "Add python.exe to PATH", then Install.
  echo  3. Double-click run_app.bat again.
  echo.
  if /i not "%~1"=="--self-test" pause
  exit /b 1
)
if /i "%~1"=="--self-test" (
  echo  The app's packages are not installed for %BASE%.
  exit /b 1
)
echo.
echo  The required packages are not installed yet. They will be installed into the folder
echo  .venv next to this file (about 2 GB to download, 10-20 minutes, internet needed once).
choice /C YN /M "Install now"
if errorlevel 2 exit /b 1
if not exist ".venv\Scripts\python.exe" ( %BASE% -m venv .venv || goto :installfail )
.venv\Scripts\python.exe -m pip install --upgrade pip || goto :installfail
.venv\Scripts\python.exe -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu || goto :installfail
.venv\Scripts\python.exe -m pip install -r requirements.txt || goto :installfail
set "PY=.venv\Scripts\python.exe"

:run
where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo  NOTE: ffmpeg was not found. Evidence cuts, playback copies and highlight videos need it:
  echo  run "winget install Gyan.FFmpeg" or put ffmpeg.exe and ffprobe.exe in packaging\ffmpeg\
)
if /i "%~1"=="--self-test" (
  %PY% -m cctv_analysis.app --self-test
  exit /b
)
echo ===================================================================
echo  Drishti-AI is starting. Your browser opens on the private link.
echo  Keep this window open while you work; close it to stop the app.
echo ===================================================================
%PY% -m cctv_analysis.app %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo  The app stopped with an error - see the messages above.
  pause
)
exit /b %RC%

:installfail
echo.
echo  Installing the packages failed - check the internet connection and run run_app.bat again.
pause
exit /b 1
