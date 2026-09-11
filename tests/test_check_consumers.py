"""Consumer discovery and missing-checkout reporting are portable contracts."""
import importlib.util
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "check_consumers.py"
spec = importlib.util.spec_from_file_location("check_consumers", SCRIPT)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def test_posix_consumer_venv_is_selected_when_registry_names_windows(tmp_path, monkeypatch):
    interpreter = tmp_path / ".venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()
    captured = []
    monkeypatch.setattr(checker.subprocess, "run", lambda args, **kwargs:
                        captured.append(args) or subprocess.CompletedProcess(args, 0, "42 passed", ""))
    result = checker.run_one({"path": str(tmp_path), "python": ".venv/Scripts/python.exe",
                              "verify": ["-m", "pytest"]})
    assert result == ("pass", "42 passed")
    assert captured[0][0] == str(interpreter)


def test_missing_consumer_is_explicitly_skipped(tmp_path):
    status, detail = checker.run_one({"path": str(tmp_path / "absent"),
                                     "python": ".venv/Scripts/python.exe"})
    assert status == "skip" and "no checkout" in detail


def test_missing_environment_does_not_claim_the_consumer_passed(tmp_path):
    status, detail = checker.run_one({"path": str(tmp_path),
                                     "python": ".venv/Scripts/python.exe"})
    assert status == "skip" and "no interpreter" in detail
