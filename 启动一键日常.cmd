@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%SCRIPT_DIR%daily_games_launcher.ps1"
endlocal
