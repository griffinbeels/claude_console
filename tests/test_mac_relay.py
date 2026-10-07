"""Actual isolated PTYs and fake CLI; Terminal automation is replaced at the UI boundary."""
import json
import os
import signal
import socket
import sys
import time
from pathlib import Path

import pytest

import claude_console
from claude_console import console_input, macos

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="Native Mac PTY relay")
FAKE = Path(__file__).with_name("_pty_fixture.py")
DESCENDANTS = Path(__file__).with_name("_pty_descendant_fixture.py")


@pytest.fixture
def terminal(tmp_path, monkeypatch):
    connections = []
    hosts = []
    screens = {}
    monkeypatch.setattr(macos, "login_environment", lambda: {
        "PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "TERM": "xterm-256color"})

    def open_fake(path):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(3)
        connection.connect(str(path))
        connection.sendall(b'{"attach":true,"rows":30,"columns":120}\n')
        connections.append(connection)
        return f"/dev/test-{len(connections)}"

    def contents(tty):
        try:
            return json.loads(screens[tty].read_text())["screen"]
        except FileNotFoundError:
            return ""

    monkeypatch.setattr(macos, "open_terminal", open_fake)
    monkeypatch.setattr(macos, "terminal_contents", contents)
    monkeypatch.setattr(console_input, "READY_TIMEOUT", .5)
    monkeypatch.setattr(console_input, "COMMAND_TIMEOUT", .2)
    monkeypatch.setattr(console_input, "ECHO_TIMEOUT", .2)
    # paste's default timeout is bound at definition; exercise real readiness
    # but keep a dialog failure below a second instead of three minutes.
    real_paste = console_input.paste
    monkeypatch.setattr(console_input, "paste", lambda pid, text:
                        real_paste(pid, text, timeout=.5, attempts=1))

    def launch(mode="ready", name="", fixture=FAKE):
        state = tmp_path / f"state-{len(hosts)}.json"
        project = tmp_path / "project with spaces 日本語"
        project.mkdir(exist_ok=True)
        opened = claude_console.open_session(project,
                    [sys.executable, str(fixture), str(state), mode], name=name,
                    environment_filter=lambda values: dict(values))
        hosts.append(opened.host)
        transport = macos._sessions[opened.pid]
        screens[transport.tty] = state
        return opened, state

    launch.connections = connections
    yield launch
    children = []
    directories = []
    for host in hosts:
        transport = macos._sessions.get(host.pid)
        if transport is not None and host.poll() is None:
            status = macos.request(transport.path, {"status": True})
            if status.get("child"):
                children.append(status["child"])
            directories.append(transport.path.parent)
    for connection in connections:
        connection.close()
    for host in hosts:
        host.wait(timeout=5)
        assert host.returncode == 0
    for pid in children:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    assert not any(path.exists() for path in directories)


def read_state(path):
    return json.loads(path.read_text())


def test_real_pty_delivers_commands_in_order_and_keeps_unicode_prompt_unsent(terminal):
    opened, state = terminal("delayed", name="Friday’s work")
    prompt = 'FEATURE: /tmp/project with spaces/任务.md\nBUG: /tmp/🚀.md'
    result = opened.deliver_now(prompt=prompt, commands=["/color green"])
    assert result.complete
    observed = read_state(state)
    assert observed["submitted"] == ["/rename Friday’s work", "/color green"]
    assert observed["prompt"] == prompt
    assert Path(observed["cwd"]).name == "project with spaces 日本語"


def test_raw_pty_write_without_application_receipt_is_not_success(terminal):
    opened, state = terminal("drop")
    result = opened.deliver_now(prompt="FEATURE: /tmp/full-path.md")
    assert result.had_prompt and not result.prompt_typed and not result.complete
    assert read_state(state)["prompt"] == ""


def test_trust_dialog_receives_no_input(terminal):
    opened, state = terminal("dialog")
    result = opened.deliver_now(prompt="FEATURE: /tmp/full-path.md", commands=["/color red"])
    assert result.commands_submitted == 0 and not result.prompt_typed
    assert read_state(state)["submitted"] == []
    assert read_state(state)["prompt"] == ""


def test_command_timeout_is_counted_and_cleared_before_prompt(terminal):
    opened, state = terminal("stuck")
    result = opened.deliver_now(prompt="FEATURE: /tmp/full-path.md", commands=["/color red", "/color blue"])
    assert result.commands_submitted == 0 and result.commands_total == 2
    assert result.prompt_typed and not result.complete
    assert read_state(state)["prompt"] == "FEATURE: /tmp/full-path.md"
    assert read_state(state)["submitted"] == []


def test_concurrent_sessions_cannot_deliver_to_each_other(terminal):
    one, state_one = terminal(name="one")
    two, state_two = terminal(name="two")
    results = []
    threads = [one.deliver("FEATURE: /tmp/one.md", on_finish=results.append),
               two.deliver("BUG: /tmp/two.md", on_finish=results.append)]
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()
    assert len(results) == 2 and all(result.complete for result in results)
    assert read_state(state_one)["prompt"] == "FEATURE: /tmp/one.md"
    assert read_state(state_two)["prompt"] == "BUG: /tmp/two.md"


def test_embedded_paste_terminator_cannot_submit_user_text(terminal):
    opened, state = terminal()
    result = opened.deliver_now(prompt="TASK\x1b[201~\r/exit")
    assert not result.prompt_typed
    assert read_state(state)["submitted"] == []


def test_closed_terminal_reports_failure_and_reaps_child(terminal, monkeypatch):
    opened, state = terminal()
    transport = macos._sessions[opened.pid]
    opened.host.terminate()
    opened.host.wait(timeout=5)
    assert not console_input.paste(opened.pid, "FEATURE: /tmp/one.md")
    assert not transport.path.exists()


def test_persons_typing_stops_retries_and_is_never_cleared_or_submitted(terminal):
    opened, state = terminal()
    assert opened.deliver_now(prompt="FEATURE: /tmp/first.md").prompt_typed
    terminal.connections[0].sendall(b"\x1b[200~ manual edits\x1b[201~")
    deadline = time.monotonic() + 2
    while "manual edits" not in read_state(state)["prompt"] and time.monotonic() < deadline:
        time.sleep(.01)
    result = opened.deliver_now(prompt="BUG: /tmp/second.md", commands=["/color red"])
    assert not result.prompt_typed and not result.commands_submitted
    assert read_state(state)["prompt"] == "FEATURE: /tmp/first.md manual edits"
    assert read_state(state)["submitted"] == []


def test_denied_terminal_permission_reaps_helper_and_removes_private_files(terminal, monkeypatch):
    directories = []
    real_mkdtemp = macos.tempfile.mkdtemp
    def capture_directory(*args, **kwargs):
        value = real_mkdtemp(*args, **kwargs)
        directories.append(Path(value))
        return value
    monkeypatch.setattr(macos.tempfile, "mkdtemp", capture_directory)
    def denied(*args):
        raise PermissionError("Terminal automation denied")
    monkeypatch.setattr(macos, "open_terminal", denied)
    with pytest.raises(PermissionError, match="Terminal automation denied"):
        terminal()
    assert directories and not any(path.exists() for path in directories)


def test_queued_terminal_input_preempts_an_automatic_write():
    """Input may arrive after select() chose a partial control request."""
    from claude_console import _mac_relay
    attached, terminal_side = socket.socketpair()
    reader, master = os.pipe()
    manual = b"\x1b[200~My unsent draft\x1b[201~"
    try:
        terminal_side.sendall(manual)
        written, generation = _mac_relay.write_automatic(
            master, attached, {"write": "\r", "input_generation": 0}, 0)
        assert written == 0 and generation == 1
        assert os.read(reader, 1024) == manual
    finally:
        attached.close()
        terminal_side.close()
        os.close(reader)
        os.close(master)


@pytest.mark.parametrize("mode", ["leader-exits-first", "leader-exits-on-termination"])
def test_relay_cleanup_stops_descendants_after_the_group_leader_exits(terminal, mode):
    opened, state = terminal(mode, fixture=DESCENDANTS)
    descendant = group = None
    try:
        deadline = time.monotonic() + 3
        while not state.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        observed = read_state(state)
        descendant, group = observed["descendant"], observed["group"]
        if mode == "leader-exits-first":
            # The direct child has exited before cleanup, not because we closed
            # the attachment. The descendant keeps the group alive by itself.
            opened.host.wait(timeout=5)
        terminal.connections[0].close()
        opened.host.wait(timeout=5)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(descendant, 0)
            except ProcessLookupError:
                break
            time.sleep(.01)
        else:
            pytest.fail("The relay exited with a live process-group descendant")
    finally:
        if descendant is not None:
            try:
                # Even the red test must not leave its disposable process up.
                if os.getpgid(descendant) == group:
                    os.kill(descendant, signal.SIGKILL)
            except ProcessLookupError:
                pass
