@echo off
chcp 65001 > nul
set PYTHONIOENCODING=utf-8
title NEXUS - Sistema Institucional Completo
echo ========================================================
echo   Iniciando NEXUS Local (Asistencia, Horarios, Sistema)
echo ========================================================
cd /d "%~dp0"

echo Abriendo navegador en http://localhost:8501 ...
start http://localhost:8501

echo Iniciando servidor local...
call .venv\Scripts\python.exe -m streamlit run app.py

pause
