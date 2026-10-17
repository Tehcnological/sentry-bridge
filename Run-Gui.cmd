@echo off
start "Sentry Bridge" /min powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "%~dp0tools\Start-Gui.ps1"
