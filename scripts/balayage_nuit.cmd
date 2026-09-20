@echo off
REM Balayage complet d'une rarete, la nuit.
REM
REM Plus d'une heure pour 46 collections, d'ou l'horaire : ce travail ne doit
REM pas se faire pendant qu'on regarde l'ecran. Au matin l'interface lit le
REM journal et affiche les contrats rentables sans rien recalculer.
REM
REM Code de sortie 10 = au moins un contrat rentable a ete trouve.

cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8

python -m tradeup.cli sweep --rarity industrial --rate 8 >> "data\balayage.log" 2>&1

echo [%date% %time%] code %ERRORLEVEL% >> "data\balayage.log"
exit /b %ERRORLEVEL%
