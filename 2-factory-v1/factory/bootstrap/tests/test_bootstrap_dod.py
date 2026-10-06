import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

BOOTSTRAP_DIR = Path(__file__).resolve().parents[1]
FIXTURES = BOOTSTRAP_DIR / "fixtures"

spec = importlib.util.spec_from_file_location("verify_bootstrap_dod", BOOTSTRAP_DIR / "verify_bootstrap_dod.py")
vbd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vbd)


from factory_v2._io import _git as _git_io


def _git_checked(cwd, *args):
    r = _git_io(cwd, *args)
    if r.returncode != 0:
        raise subprocess.CalledProcessError(r.returncode, ["git", *args], r.stdout, r.stderr)
    return r


def make_candidate_repo(tmp_path: Path, fixture_name: str) -> tuple[Path, str]:
    repo = tmp_path / "candidate_repo"
    shutil.copytree(FIXTURES / fixture_name, repo)
    _git_checked(repo, "init", "-q")
    _git_checked(repo, "config", "user.email", "test@example.com")
    _git_checked(repo, "config", "user.name", "Test")
    _git_checked(repo, "add", ".")
    _git_checked(repo, "commit", "-q", "-m", "candidate")
    sha = _git_checked(repo, "rev-parse", "HEAD").stdout.strip()
    return repo, sha


def make_isolated_lock(tmp_path: Path) -> tuple[Path, Path]:
    """A synthetic bootstrap tree + lock, fully isolated from the real repo — so lock-tamper
    tests never touch this factory's own real BOOTSTRAP_LOCK.json."""
    base = tmp_path / "synthetic_bootstrap_base"
    locked_file = base / "locked_thing.py"
    locked_file.parent.mkdir(parents=True, exist_ok=True)
    locked_file.write_text("VALUE = 1\n")
    lock = {"files": {"locked_thing.py": vbd.sha256_file(locked_file)}}
    lock_path = tmp_path / "BOOTSTRAP_LOCK.json"
    lock_path.write_text(json.dumps(lock, indent=2))
    return base, lock_path


def test_bootstrap_lock_valid_on_untouched_files(tmp_path):
    base, lock_path = make_isolated_lock(tmp_path)
    ok, problems = vbd.verify_bootstrap_lock(lock_path, base=base)
    assert ok and not problems


def test_bootstrap_rejects_modified_locked_file(tmp_path):
    base, lock_path = make_isolated_lock(tmp_path)
    (base / "locked_thing.py").write_text("VALUE = 999  # tampered\n")
    ok, problems = vbd.verify_bootstrap_lock(lock_path, base=base)
    assert not ok
    assert any("BOOTSTRAP_LOCK_VIOLATION" in p for p in problems)


def test_bootstrap_rejects_missing_locked_file(tmp_path):
    base, lock_path = make_isolated_lock(tmp_path)
    (base / "locked_thing.py").unlink()
    ok, problems = vbd.verify_bootstrap_lock(lock_path, base=base)
    assert not ok
    assert any("missing" in p for p in problems)


def test_full_run_accepts_good_candidate_and_writes_pass_proof(tmp_path):
    repo, sha = make_candidate_repo(tmp_path, "good_candidate")
    _, lock_path = make_isolated_lock(tmp_path)
    proof_dir = tmp_path / "external_proof"
    rc = vbd.run(repo, sha, proof_dir, lock_path, lock_base=(tmp_path / "synthetic_bootstrap_base"))
    assert rc == 0
    pass_file = proof_dir / "M0_SELF_HOST_PASS.json"
    assert pass_file.exists()
    receipt = json.loads(pass_file.read_text())
    assert receipt["all_pass"] is True
    assert receipt["candidate_sha"] == sha
    assert receipt["verifier"] == "verify_bootstrap_dod.py"


def test_full_run_rejects_bad_candidate_writes_no_proof(tmp_path):
    repo, sha = make_candidate_repo(tmp_path, "bad_candidate")
    _, lock_path = make_isolated_lock(tmp_path)
    proof_dir = tmp_path / "external_proof"
    rc = vbd.run(repo, sha, proof_dir, lock_path, lock_base=(tmp_path / "synthetic_bootstrap_base"))
    assert rc == 1
    assert not (proof_dir / "M0_SELF_HOST_PASS.json").exists()


def test_bootstrap_candidate_cannot_write_pass_proof(tmp_path):
    """bootstrap_gate.py (the candidate-side, non-authoritative runner) is never given the
    proof-dir path and has no code path that references M0_SELF_HOST_PASS.json at all."""
    gate_src = (BOOTSTRAP_DIR / "bootstrap_gate.py").read_text()
    # Structural check: no --proof-dir CLI argument and no write call targeting that filename —
    # the docstring is allowed to explain WHY it doesn't do this (see module docstring above).
    assert "--proof-dir" not in gate_src
    assert "M0_SELF_HOST_PASS.json" not in gate_src.split('"""', 2)[-1], \
        "bootstrap_gate.py's executable code (outside its docstring) must never name the pass-proof file"
    repo, sha = make_candidate_repo(tmp_path, "good_candidate")
    scratch = tmp_path / "gate_scratch"
    cp = subprocess.run(
        ["python3", str(BOOTSTRAP_DIR / "bootstrap_gate.py"), "--repo", str(repo),
         "--candidate-sha", sha, "--scratch", str(scratch)],
        capture_output=True, text=True,
    )
    assert cp.returncode == 0
    # bootstrap_gate.py has no proof_dir argument at all — nothing outside its own scratch changed
    assert not (tmp_path / "external_proof").exists()


def test_bootstrap_rejects_modified_external_verifier(tmp_path):
    """If verify_bootstrap_dod.py itself is among the locked files and gets modified after the
    lock was cut, BOOTSTRAP_LOCK_VIOLATION fires before any self-host check runs."""
    base = tmp_path / "verifier_base"
    base.mkdir()
    verifier_copy = base / "verify_bootstrap_dod.py"
    shutil.copy(BOOTSTRAP_DIR / "verify_bootstrap_dod.py", verifier_copy)
    lock = {"files": {"verify_bootstrap_dod.py": vbd.sha256_file(verifier_copy)}}
    lock_path = tmp_path / "BOOTSTRAP_LOCK.json"
    lock_path.write_text(json.dumps(lock))
    # simulate a candidate that tampered with the locked verifier after the lock was cut
    verifier_copy.write_text(verifier_copy.read_text() + "\n# tampered\n")
    ok, problems = vbd.verify_bootstrap_lock(lock_path, base=base)
    assert not ok
    assert any("verify_bootstrap_dod.py" in p for p in problems)


def test_bootstrap_rejects_candidate_weakened_factory_oracle(tmp_path):
    """A candidate whose own tests/test_verify_factory_dod.py component check fails (e.g. because
    it weakened verify_factory_dod.py so that file's own tests no longer pass) must not reach
    all_pass, even if its other tests are green."""
    repo = tmp_path / "weakened_candidate"
    shutil.copytree(FIXTURES / "good_candidate", repo)
    (repo / "tests" / "test_verify_factory_dod.py").write_text(
        "def test_weakened():\n    assert False, 'candidate broke the factory oracle tests'\n"
    )
    _git_checked(repo, "init", "-q")
    _git_checked(repo, "config", "user.email", "test@example.com")
    _git_checked(repo, "config", "user.name", "Test")
    _git_checked(repo, "add", ".")
    _git_checked(repo, "commit", "-q", "-m", "candidate")
    sha = _git_checked(repo, "rev-parse", "HEAD").stdout.strip()
    _, lock_path = make_isolated_lock(tmp_path)
    proof_dir = tmp_path / "external_proof"
    rc = vbd.run(repo, sha, proof_dir, lock_path, lock_base=(tmp_path / "synthetic_bootstrap_base"))
    assert rc == 1
    assert not (proof_dir / "M0_SELF_HOST_PASS.json").exists()


def test_old_m0_pass_is_invalidated_after_root_change(tmp_path):
    proof_dir = tmp_path / "external_proof"
    proof_dir.mkdir()
    old_receipt = {"schema": "factory-bootstrap-receipt/v1", "candidate_sha": "a" * 40, "all_pass": True}
    (proof_dir / "M0_SELF_HOST_PASS.json").write_text(json.dumps(old_receipt))
    vbd.invalidate_old_pass(proof_dir)
    assert (proof_dir / "M0_SELF_HOST_PASS.json").exists(), "original evidence must never be deleted"
    marker = json.loads((proof_dir / "M0_SELF_HOST_PASS.INVALIDATED.json").read_text())
    assert marker["reason_code"] == "BOOTSTRAP_ROOT_OF_TRUST_CHANGED"
    assert marker["original_receipt"]["candidate_sha"] == "a" * 40


def test_no_double_writer_rejects_live_lock(tmp_path):
    lock_file = tmp_path / "writer.LOCK"
    lock_file.write_text(json.dumps({"updated_utc": vbd.utc_now()}))
    ok, reason = vbd.check_no_double_writer(lock_file, stale_after_seconds=240)
    assert not ok and "REJECT_DOUBLE_WRITER" in reason


def test_no_double_writer_accepts_stale_lock(tmp_path):
    import datetime as dt
    lock_file = tmp_path / "writer.LOCK"
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=999)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    lock_file.write_text(json.dumps({"updated_utc": old}))
    ok, reason = vbd.check_no_double_writer(lock_file, stale_after_seconds=240)
    assert ok


def test_receipt_without_matching_event_rejected(tmp_path):
    events = tmp_path / "events.jsonl"
    events.write_text(json.dumps({"event_hash": "real_hash_123"}) + "\n")
    fake_receipt = {"factory_event_hash": "fabricated_hash_999"}
    ok, reason = vbd.check_receipt_matches_event(fake_receipt, events)
    assert not ok and "REJECT" in reason


def test_receipt_with_matching_event_accepted(tmp_path):
    events = tmp_path / "events.jsonl"
    events.write_text(json.dumps({"event_hash": "real_hash_123"}) + "\n")
    real_receipt = {"factory_event_hash": "real_hash_123"}
    ok, reason = vbd.check_receipt_matches_event(real_receipt, events)
    assert ok


def test_real_bootstrap_lock_is_currently_valid():
    """Regression guard: the real, committed BOOTSTRAP_LOCK.json must validate against the
    real bootstrap files as they currently stand on disk. Also guards against the pycache-glob
    bug found on this test file's first live run (a stray .pyc briefly polluted the lock)."""
    lock_path = BOOTSTRAP_DIR / "BOOTSTRAP_LOCK.json"
    ok, problems = vbd.verify_bootstrap_lock(lock_path, base=vbd.REPO_ROOT)
    assert ok, problems
    lock = json.loads(lock_path.read_text())
    assert not any(f.endswith(".pyc") or "__pycache__" in f for f in lock["files"])
