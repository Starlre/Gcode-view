@echo off
setlocal
set ELECTRON_RUN_AS_NODE=
cd /d "%~dp0electron"
if exist "release\win-unpacked\HKUprint Studio.exe" (
    start "" "release\win-unpacked\HKUprint Studio.exe"
    exit /b
)
if not exist "node_modules\electron\dist\electron.exe" (
    echo Please run npm install in the electron folder first.
    pause
    exit /b 1
)
call npm.cmd start
if errorlevel 1 pause
