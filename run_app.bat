@echo off
title CCTV Analysis
cd /d "%~dp0"
echo ===================================================
echo Starting CCTV Analysis Desktop Application...
echo The browser will open automatically.
echo Keep this window open while using the application.
echo ===================================================
python -m cctv_analysis.app
pause
