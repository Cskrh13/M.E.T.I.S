@echo off
chcp 65001 >nul
title Agent 4.2 - Grand Planificateur (ne pas fermer)
cd /d "%~dp0"
set PY=H:\GrandPlanificateur\moteur\python\python.exe
if not exist "%PY%" set PY=python
:boucle
"%PY%" agent4_2.py
if exist "arret.txt" goto fin
echo.
echo [%date% %time%] L'agent s'est arrete. Relance dans 20 secondes (creer un fichier arret.txt pour stopper).
timeout /t 20 /nobreak >nul
goto boucle
:fin
del "arret.txt"
echo Arret demande. Au revoir.
pause
