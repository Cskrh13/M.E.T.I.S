@echo off
chcp 65001 >nul
title Grand Planificateur - process racine (ne pas fermer)
cd /d "%~dp0"
set PY=H:\GrandPlanificateur\moteur\python\python.exe
if not exist "%PY%" set PY=python
"%PY%" grand_planificateur.py --ouvrir
pause
