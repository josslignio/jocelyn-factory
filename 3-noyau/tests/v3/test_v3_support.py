"""Preuves des primitives autonomes de V3 — aucun import ni processus V2."""
from __future__ import annotations

import ast
import json
import os
import subprocess
from pathlib import Path

from factory_v3 import run as noyau
from factory_v3 import support


def test_v3_ne_depend_plus_de_factory_v2():
    for source in Path("factory_v3").glob("*.py"):
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            modules = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                       else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            assert not any(name == "factory_v2" or name.startswith("factory_v2.") for name in modules)


def test_atomic_write_est_durable_et_sans_reste(tmp_path, monkeypatch):
    target, fsyncs = tmp_path / "a" / "receipt.json", []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (fsyncs.append(fd), real_fsync(fd))[1])
    support.atomic_write(target, {"verdict": "VERT"})
    assert json.loads(target.read_text()) == {"verdict": "VERT"}
    assert len(fsyncs) >= 2
    assert list(target.parent.glob("*.tmp")) == []


def test_read_json_echoue_ferme(tmp_path):
    assert support.read_json(tmp_path / "absent") is None
    (tmp_path / "bad").write_text("[")
    assert support.read_json(tmp_path / "bad") is None
    (tmp_path / "list").write_text("[]")
    assert support.read_json(tmp_path / "list") is None


def test_etat_runtime_est_partage_par_tous_les_worktrees(tmp_path):
    repo, autre = tmp_path / "repo", tmp_path / "autre"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=t@x",
                    "commit", "--allow-empty", "-qm", "base"], check=True)
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", "-q", str(autre)], check=True)
    assert noyau.runtime_base(repo) == noyau.runtime_base(autre)
