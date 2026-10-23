@echo off
rem MyHealth: one-command start (double-click or run "start.bat")
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
if errorlevel 1 pause
