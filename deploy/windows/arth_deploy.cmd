@echo off
title Arth server install
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0arth_deploy.ps1"
echo.
pause
