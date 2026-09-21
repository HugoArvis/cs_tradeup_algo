@echo off
REM Balayage complet d'une rarete, la nuit.
REM
REM Plus d'une heure pour 46 collections, d'ou l'horaire : ce travail ne doit
REM pas se faire pendant qu'on regarde l'ecran. Au matin l'interface lit le
REM journal et affiche les contrats rentables sans rien recalculer.
REM
REM Code de sortie 10 = au moins un contrat rentable a ete trouve.
REM
REM Cible : MIL-SPEC et non Industrial. Une caisse ne descend jamais sous le
REM Mil-Spec, donc l'Industrial n'a que 46 collections de carte quand le
REM Mil-Spec en a 88, dont 39 issues de caisses. Deux jours de mesures en
REM Industrial n'ont rien donne de rentable -- c'est la zone la plus pauvre
REM du jeu, pas un hasard.

cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8

python -m tradeup.cli sweep --rarity mil-spec --rate 8 >> "data\balayage.log" 2>&1

echo [%date% %time%] code %ERRORLEVEL% >> "data\balayage.log"
exit /b %ERRORLEVEL%
