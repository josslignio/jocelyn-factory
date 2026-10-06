"""Garde d'espace disque — FACTORY_REPO_HYGIENE_V1 (22/09).

Le 21/09 la promotion d'un jalon a échoué sur « No space left on device » au milieu d'un `git worktree add`
(114 861 dossiers temporaires, 32 worktrees archivés). Désormais tout créateur de worktree (dispatcher,
worktree_manager, promotion dorée) appelle `ensure_free_gb(path, min_gb)` AVANT `git worktree add` et refuse avec
un événement typé `DISK_SPACE_LOW` (`DiskSpaceLow.to_event()`), jamais un crash au milieu d'une opération git.

`FACTORY_MIN_FREE_GB` (flottant, en Gio) remplace `min_gb` quand il est défini : un banc hermétique sur une machine
presque pleine le met à 0 ; la production garde 10 Gio. `FACTORY_FREE_GB_OVERRIDE` (tests seulement) impose la
valeur mesurée pour simuler un disque plein sans le remplir.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

EVENT = "DISK_SPACE_LOW"
MIN_FREE_GB_DEFAULT = 10.0
_ENV_MIN = "FACTORY_MIN_FREE_GB"
_ENV_FREE_OVERRIDE = "FACTORY_FREE_GB_OVERRIDE"


class DiskSpaceLow(RuntimeError):
    """Refus typé : `event` = DISK_SPACE_LOW ; `to_event()` = l'événement prêt pour le journal (JSON)."""

    event = EVENT

    def __init__(self, path: "Path | str", free_gb: float, min_gb: float) -> None:
        self.path, self.free_gb, self.min_gb = Path(path), float(free_gb), float(min_gb)
        super().__init__(f"{EVENT}: {self.free_gb:.2f} Gio libres sous {self.path} < {self.min_gb:.2f} Gio requis")

    def to_event(self) -> dict:
        return {"event": EVENT, "path": str(self.path), "free_gb": round(self.free_gb, 3),
                "min_gb": round(self.min_gb, 3), "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def _existing_ancestor(path: Path) -> Path:
    p = Path(path).resolve()
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def free_gb(path: "Path | str") -> float:
    """Gio libres sur le volume qui portera `path` (ou son ancêtre existant le plus proche)."""
    raw = os.environ.get(_ENV_FREE_OVERRIDE)
    if raw not in (None, ""):
        try:
            return float(raw)
        except ValueError:
            pass
    return shutil.disk_usage(_existing_ancestor(Path(path))).free / float(1 << 30)


def min_free_gb(default: float = MIN_FREE_GB_DEFAULT) -> float:
    """Seuil effectif : FACTORY_MIN_FREE_GB s'il est un flottant valide, sinon `default`."""
    raw = os.environ.get(_ENV_MIN)
    if raw not in (None, ""):
        try:
            return float(raw)
        except ValueError:
            pass
    return float(default)


def ensure_free_gb(path: "Path | str", min_gb: float = MIN_FREE_GB_DEFAULT) -> float:
    """Retourne les Gio libres sous `path`, ou lève DiskSpaceLow (événement DISK_SPACE_LOW) s'ils sont < min_gb."""
    threshold = min_free_gb(min_gb)
    free = free_gb(path)
    if free < threshold:
        raise DiskSpaceLow(Path(path), free, threshold)
    return free
