"""Synthetic launch-boundary checks; no Terminal, login profile or child process."""
import json
from pathlib import Path
import sys

import pytest
from claude_console import macos, session


def test_shared_mac_dispatch_preserves_the_selected_filter(monkeypatch, tmp_path):
    selected = lambda values: {"PATH": values["PATH"]}
    observed = {}
    monkeypatch.setattr(session, "_PLATFORM", "darwin")
    monkeypatch.setattr(macos, "spawn_claude", lambda cwd, launch, name, **kwargs:
                        observed.update(kwargs) or "synthetic-host")
    assert session.spawn_claude(tmp_path, environment_filter=selected) == "synthetic-host"
    assert observed["environment_filter"] is selected


def test_mac_saved_configuration_and_relay_receive_only_filtered_values(monkeypatch, tmp_path):
    monkeypatch.setattr(macos, "login_environment", lambda: {
        "PATH": "synthetic-path", "CANARY_SECRET": "fixture-only"})
    directory = tmp_path / "relay"
    directory.mkdir()
    monkeypatch.setattr(macos.tempfile, "mkdtemp", lambda **kwargs: str(directory))
    observed = {}

    def fake_process(argv, **kwargs):
        observed["configuration"] = json.loads(Path(argv[-1]).read_text())
        observed["process_environment"] = kwargs["env"]
        raise RuntimeError("synthetic process boundary")

    monkeypatch.setattr(macos.subprocess, "Popen", fake_process)
    with pytest.raises(RuntimeError, match="synthetic process boundary"):
        macos.spawn_claude(tmp_path, [sys.executable],
                          environment_filter=lambda values: {"PATH": values["PATH"]})
    assert observed["configuration"]["env"] == {"PATH": "synthetic-path"}
    assert observed["process_environment"] == {"PATH": "synthetic-path"}
    assert not directory.exists()


@pytest.mark.parametrize("result", [None, {"PATH": 4}, ["PATH"]])
def test_invalid_mac_filter_creates_neither_configuration_nor_process(monkeypatch, tmp_path, result):
    monkeypatch.setattr(macos, "login_environment", lambda: {"PATH": "synthetic-path"})
    monkeypatch.setattr(macos.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("process created"))
    monkeypatch.setattr(macos.tempfile, "mkdtemp", lambda **kwargs: pytest.fail("configuration created"))
    with pytest.raises(ValueError, match="string mapping"):
        macos.spawn_claude(tmp_path, [sys.executable], environment_filter=lambda values: result)


def test_unavailable_mac_filter_creates_neither_configuration_nor_process(monkeypatch, tmp_path):
    monkeypatch.setattr(macos, "login_environment", lambda: {"PATH": "synthetic-path"})
    monkeypatch.setattr(macos.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("process created"))
    monkeypatch.setattr(macos.tempfile, "mkdtemp", lambda **kwargs: pytest.fail("configuration created"))

    def unavailable(values):
        raise ValueError("Credential protection unavailable")

    with pytest.raises(ValueError, match="unavailable"):
        macos.spawn_claude(tmp_path, [sys.executable], environment_filter=unavailable)
