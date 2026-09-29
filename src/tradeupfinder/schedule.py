"""Enregistrement du passage quotidien aupres du planificateur du systeme.

La planification appartient au projet, pas a l'outil qui l'a ecrit. Une tache
portee par une session -- un terminal ouvert, un assistant en cours -- meurt
avec elle, et un modele qui ne tourne que pendant qu'on le regarde ne sert a
rien : c'est justement entre deux consultations que les prix bougent.

Ce module ne fait que CONSTRUIRE les commandes ; le CLI les execute. La
separation a une raison : une commande d'enregistrement se verifie par un test
sans jamais toucher au systeme.

Deux passages par jour, pas un. Les fenetres Steam se referment pour des
heures et se rouvrent sans prevenir -- observe le 19 septembre 2026 : neuf
refus consecutifs de 04:38 a 11:20, puis une fenetre ouverte a 11:35. Un
creneau fixe unique rate le marche la moitie du temps ; deux creneaux eloignes
divisent ce risque sans doubler la consommation, puisque le second constate
que les candidats de tete sont deja frais.

L'enregistrement passe par `Register-ScheduledTask` et non par `schtasks`,
pour une raison mesuree sur cette machine : `schtasks /create` laisse
`DisallowStartIfOnBatteries` a vrai. La tache est alors mise en file et n'est
jamais executee tant que le portable est sur batterie -- etat "Queued",
dernier resultat 0, aucune sortie, aucune erreur. Un echec quotidien
parfaitement silencieux.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

#: Heures des deux passages. Volontairement decalees des heures rondes : rien
#: ne l'impose ici, mais un creneau partage avec tout le monde n'aide pas.
PASSAGES = (("matin", "07:47"), ("soir", "19:23"))

#: Le balayage complet, lui, dure plus d'une heure : il tourne la nuit, une
#: seule fois, sur un lanceur different. Le separer des passages courts evite
#: qu'un travail long bloque un travail frequent -- et inversement.
BALAYAGE = ("balayage", "03:11", "balayage_nuit.cmd")

#: Prefixe des taches creees, pour pouvoir les retrouver et les retirer.
PREFIXE = "tradeupfinder-quotidien"

#: Prefixes utilises AVANT un renommage du projet. Les taches deja enregistrees
#: dans Windows gardent leur ancien nom : elles continuent de tourner, mais le
#: code ne les reconnait plus. Sans cette liste, `schedule` les croit absentes
#: et en enregistre de nouvelles -- soit DEUX passages a 07:47, chacun consommant
#: le quota Steam de l'autre. Le defaut ne se voit pas : les deux taches
#: reussissent.
ANCIENS_PREFIXES = ("tradeup-quotidien",)

#: Au-dela, le passage est considere comme bloque et arrete. Large, car une
#: fenetre Steam ouverte peut demander plus d'une heure a 4 requetes/minute.
LIMITE_HEURES = 3


def _ps(commande: str) -> list[str]:
    """Enveloppe une commande PowerShell, sans profil ni interaction."""
    return ["powershell", "-NoProfile", "-NonInteractive", "-Command", commande]


@dataclass(frozen=True, slots=True)
class Tache:
    """Une tache planifiee, telle qu'on l'enregistrera."""

    nom: str
    heure: str
    lanceur: Path

    def creer(self) -> list[str]:
        """Commande d'enregistrement.

        Trois reglages ne sont pas des details :

        - `AllowStartIfOnBatteries` et `DontStopIfGoingOnBatteries` : sans eux
          la tache ne demarre pas sur batterie, ou s'arrete en cours de route.
        - `StartWhenAvailable` : si la machine dormait a l'heure dite, le
          passage est rattrape au reveil. Sans lui, une nuit d'absence fait
          sauter la journee entiere, et la serie du journal -- qui n'a de
          valeur que par sa continuite -- se troue.
        - `-Force` : reinstaller apres modification du lanceur doit remplacer
          la tache, pas echouer en laissant tourner l'ancienne version.
        """
        return _ps(
            f"$a = New-ScheduledTaskAction -Execute '{self.lanceur}'; "
            f"$t = New-ScheduledTaskTrigger -Daily -At '{self.heure}'; "
            f"$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries "
            f"-DontStopIfGoingOnBatteries -StartWhenAvailable "
            f"-ExecutionTimeLimit (New-TimeSpan -Hours {LIMITE_HEURES}); "
            f"Register-ScheduledTask -TaskName '{self.nom}' -Action $a "
            f"-Trigger $t -Settings $s -Force | Out-Null"
        )

    def supprimer(self) -> list[str]:
        return _ps(
            f"Unregister-ScheduledTask -TaskName '{self.nom}' -Confirm:$false"
        )

    def etat(self) -> list[str]:
        """Etat lisible : la tache existe-t-elle, et peut-elle reellement partir ?

        On verifie aussi le reglage batterie : une tache presente mais bloquee
        en file n'est pas une tache qui tourne.
        """
        return _ps(
            f"$t = Get-ScheduledTask -TaskName '{self.nom}' -ErrorAction Stop; "
            f"$i = $t | Get-ScheduledTaskInfo; "
            f"if ($t.Settings.DisallowStartIfOnBatteries) "
            f"{{ Write-Output 'BATTERIE' }} "
            f"else {{ Write-Output ('OK ' + $i.NextRunTime) }}"
        )


def lanceur(racine: Path | None = None) -> Path:
    """Chemin du script que le planificateur appellera."""
    base = racine or Path(__file__).resolve().parents[2]
    return base / "scripts" / "passage_quotidien.cmd"


def survivantes() -> list[str]:
    """Commande listant les taches restees sous un ANCIEN prefixe.

    A appeler apres un renommage du projet : ces taches tournent toujours et
    lanceraient un second passage en plus des nouvelles. Elles ne se signalent
    pas d'elles-memes -- elles reussissent.
    """
    motifs = ",".join(f"'{p}-*'" for p in ANCIENS_PREFIXES)
    return _ps(
        f"Get-ScheduledTask -TaskName {motifs} -ErrorAction SilentlyContinue | "
        f"Select-Object -ExpandProperty TaskName"
    )


def retire_ancienne(nom: str) -> list[str]:
    """Commande retirant une tache laissee par un ancien nom de projet."""
    return _ps(f"Unregister-ScheduledTask -TaskName '{nom}' -Confirm:$false")


def taches(racine: Path | None = None) -> list[Tache]:
    base = racine or Path(__file__).resolve().parents[2]
    chemin = lanceur(racine)
    libelle_b, heure_b, script_b = BALAYAGE
    return [
        *(Tache(f"{PREFIXE}-{libelle}", heure, chemin)
          for libelle, heure in PASSAGES),
        Tache(f"{PREFIXE}-{libelle_b}", heure_b, base / "scripts" / script_b),
    ]


def supporte() -> bool:
    """Le planificateur de ce systeme est-il celui qu'on sait piloter ?"""
    return sys.platform == "win32"


def equivalent_cron(racine: Path | None = None) -> str:
    """Lignes crontab equivalentes, pour un systeme non Windows.

    Mieux vaut donner de quoi faire a la main que refuser sans rien proposer.
    """
    chemin = lanceur(racine).with_suffix(".sh")
    lignes = []
    for libelle, heure in PASSAGES:
        h, m = heure.split(":")
        lignes.append(f"{int(m)} {int(h)} * * *  {chemin}  # {PREFIXE}-{libelle}")
    return "\n".join(lignes)
