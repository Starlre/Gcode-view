@echo off
setlocal
cd /d "%~dp0"
python gcode_viewer_3d.py %*
if errorlevel 1 pause
