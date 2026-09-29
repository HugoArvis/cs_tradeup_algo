@echo off
REM Passage quotidien : recote les candidats de tete, classe, journalise.
REM
REM A enregistrer dans le Planificateur de taches Windows pour qu'il tourne
REM sans Claude ni terminal ouvert :
REM
REM   schtasks /create /tn "tradeupfinder-quotidien-matin" /tr "\"%~f0\"" /sc daily /st 07:47
REM
REM Code de sortie 10 = un contrat est rentable ET sur des prix frais.
REM Le Planificateur le consigne : filtrer sur ce code suffit a etre alerte
REM sans relire la sortie.

cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8

REM --rate 4 : au-dela, Steam ferme sa porte pour des heures. Le debit n'est
REM pas un reglage de confort, c'est la contrainte qui decide si le passage
REM ramene quelque chose.
python -m tradeupfinder.cli daily --rarity industrial --rate 4 --budget 200 ^
    --top 5 --limit 15 >> "data\passages.log" 2>&1

echo [%date% %time%] code %ERRORLEVEL% >> "data\passages.log"
exit /b %ERRORLEVEL%
