@echo off
setlocal
cd /d "%~dp0"
python gcode_slicer.py %*
if errorlevel 1 pause
