"""Default guard loads only the installed trusted helper; no processes or secrets."""
from pathlib import Path
import subprocess
import pytest
from claude_console import session


def test_missing_default_helper_prevents_process_creation(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(session, 'claude_environment', lambda: {'PATH': 'synthetic'})
    spawned = []
    monkeypatch.setattr(subprocess, 'Popen', lambda *args, **kwargs: spawned.append(True))
    with pytest.raises(ValueError, match='Credential protection is unavailable'):
        session.spawn_claude(tmp_path)
    assert not spawned


def test_default_guard_uses_fixed_trusted_path_before_spawn(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    helper = tmp_path / '.claude/harness/harness/credential_safety.py'
    helper.parent.mkdir(parents=True)
    helper.write_text("def sanitize_environment(values):\n    return {k:v for k,v in values.items() if k == 'PATH'}\n", encoding='utf-8')
    monkeypatch.setattr(session, 'claude_environment', lambda: {'PATH': 'synthetic', 'CANARY_SECRET': 'fixture-only'})
    captured = {}
    monkeypatch.setattr(subprocess, 'Popen', lambda *args, **kwargs: captured.update(kwargs))
    session.spawn_claude(tmp_path)
    assert captured['env'] == {'PATH': 'synthetic'}
