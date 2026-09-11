"""Mac contracts; native windows are never opened by this suite."""
import subprocess
import sys

import pytest

from claude_console import console_input, session

mac_native = pytest.mark.skipif(sys.platform != "darwin", reason="Native Mac shell")


def test_public_import_and_cli_help_need_no_native_window():
    result = subprocess.run([sys.executable, "-m", "claude_console", "--help"],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "--name" in result.stdout
    assert console_input.Delivery(0, 0, False, False, 0).complete


@mac_native
def test_mac_launch_carries_name_as_one_argument(monkeypatch, tmp_path):
    from claude_console import macos
    argv = macos.default_launch('Friday’s tasks; touch /tmp/not-a-command')
    assert argv[:4] == ["/bin/zsh", "-l", "-i", "-c"]
    # Read the shell's actual argv using a harmless stand-in, not its source.
    script = argv[4].removesuffix("; exec /bin/zsh -l")
    script = script.replace("claude", "printf '%s\\n'", 1)
    result = subprocess.run([*argv[:4], script], capture_output=True, text=True,
                            timeout=10, env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["--dangerously-skip-permissions", "-n",
                                         session.display_name('Friday’s tasks; touch /tmp/not-a-command')]


@mac_native
def test_mac_environment_does_not_inherit_agent_variables(monkeypatch, tmp_path):
    from claude_console import macos
    import pwd
    from types import SimpleNamespace
    account = SimpleNamespace(pw_dir=str(tmp_path), pw_name="console-test", pw_shell="/bin/zsh")
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: account)
    (tmp_path / ".zshrc").write_text('echo "A profile greeting"\nexport CONSOLE_SHELL_PROFILE="loaded"\n', encoding="utf-8")
    monkeypatch.setenv("SPAWNER_ONLY_VAR", "must not reach session")
    monkeypatch.setenv("NO_COLOR", "1")
    result = macos.login_environment()
    assert "SPAWNER_ONLY_VAR" not in result
    assert "NO_COLOR" not in result
    assert result["CONSOLE_SHELL_PROFILE"] == "loaded"
    assert all(result[key] for key in ("HOME", "USER", "PATH", "SHELL", "TERM"))


@mac_native
def test_mac_missing_override_fails_before_opening_terminal(tmp_path, monkeypatch):
    from claude_console import macos
    monkeypatch.setattr(macos, "open_terminal", lambda *a: pytest.fail("opened a window"))
    with pytest.raises(FileNotFoundError):
        macos.spawn_claude(tmp_path, ["/does/not/exist"])


def test_screen_confirmation_rejects_shell_echo_and_dialogs():
    from claude_console import macos
    assert not macos.prompt_ready("$ echo '? for shortcuts'\n? for shortcuts")
    assert not macos.prompt_ready("❯ yes\nDo you trust this folder?\n? for shortcuts")
    assert macos.prompt_ready("❯\xa0\n? for shortcuts")


def test_terminal_carriage_return_rows_are_normalized_before_first_write(monkeypatch):
    from claude_console import macos
    from pathlib import Path
    monkeypatch.setattr(macos, "terminal_contents", lambda tty: "❯\xa0\r? for shortcuts")
    payload = console_input.PASTE_START + "TASK" + console_input.PASTE_END
    monkeypatch.setattr(macos, "request", lambda path, message:
                        {"input_generation": 0} if message.get("status") else {"written": len(message["write"].encode())})
    transport = macos.Transport(None, Path("unused"), "/dev/test")
    assert transport.write(payload)
