@echo off
rem Mira scene - one command start (calls start.ps1 which bootstraps .venv + deps).
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
