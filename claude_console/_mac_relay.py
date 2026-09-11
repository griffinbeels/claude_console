"""Separate stdlib PTY owner and Terminal attachment. Never import into Cocoa.

The GUI starts ``serve`` in a new interpreter. Terminal runs ``attach`` and
provides its own input/output and dimensions. Closing that connection closes
the child PTY and reaps its process group. Exiting the GUI does not disconnect
the user's terminal. No subprocess here opens a window or starts real Claude
unless the user-facing caller explicitly supplied that launch.
"""
import json
import os
import select
import signal
import socket
import sys
import time
from pathlib import Path

MAX_MESSAGE = 1024 * 1024


def read_message(connection):
    # Do not buffer past the handshake: attach switches to a raw byte stream.
    data = bytearray()
    while len(data) < MAX_MESSAGE:
        chunk = connection.recv(1)
        if not chunk:
            raise EOFError("connection closed")
        if chunk == b"\n":
            return json.loads(data)
        data.extend(chunk)
    raise ValueError("relay message too large")


def write_all(fd, data):
    view = memoryview(data)
    while view:
        count = os.write(fd, view)
        if not count:
            raise OSError("PTY write stopped")
        view = view[count:]


def write_automatic(master, attached, message, input_generation):
    """Refuse a control write if raw terminal input arrived while it was read."""
    if select.select([attached], [], [], 0)[0]:
        incoming = attached.recv(65536)
        if not incoming:
            raise EOFError("terminal closed")
        input_generation += 1
        write_all(master, incoming)
    if message.get("input_generation") != input_generation:
        return 0, input_generation
    text = message.get("write")
    if not isinstance(text, str):
        return 0, input_generation
    # Only explicit control keys and a complete paste enter this path.
    # An embedded paste terminator cannot escape into shell/command input.
    content = text[6:-6] if text.startswith("\x1b[200~") and text.endswith("\x1b[201~") else None
    safe = (text in ("\r", "\x15", "\x1b[201~") or
            content is not None and all(ord(c) >= 32 or c in "\n\t" for c in content))
    if not safe:
        return 0, input_generation
    encoded = text.encode("utf-8")
    write_all(master, encoded)
    return len(encoded), input_generation


def resize(fd, rows, columns):
    import termios
    termios.tcsetwinsize(fd, (max(1, min(int(rows), 1000)),
                              max(1, min(int(columns), 1000))))


def _stop_child(pid):
    if pid is None:
        return
    try:
        if os.waitpid(pid, os.WNOHANG)[0]:
            return
    except ChildProcessError:
        return
    # The child owns its process group. Never search process names or kill
    # unrelated sessions; reap even when the PTY already reported EOF.
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError:
        # Closing the PTY can end the group before this signal reaches it;
        # macOS reported EPERM here during concurrent-session cleanup. Signal
        # only our known child directly, then reap it in the same bounded loop.
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            if os.waitpid(pid, os.WNOHANG)[0]:
                return
        except ChildProcessError:
            return
        time.sleep(.02)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass


def serve(config_path):
    import pty  # This process has never loaded Cocoa or started a GUI thread.
    config_path = Path(config_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    path = config_path.with_name("relay.sock")
    child = master = attached = None
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    bracketed = False
    output_tail = b""
    input_generation = 0
    deadline = time.monotonic() + 60

    def interrupted(*_):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    try:
        listener.bind(str(path))
        os.chmod(path, 0o600)
        listener.listen(8)
        print("ready", flush=True)
        while True:
            if attached is None and time.monotonic() > deadline:
                return
            sources = ([attached, master] if attached is not None else []) + [listener]
            readable, _, _ = select.select(sources, [], [], .2)
            for source in readable:
                if source is listener:
                    connection, _ = listener.accept()
                    connection.settimeout(5)
                    try:
                        message = read_message(connection)
                        if message.get("attach") and attached is None:
                            child, master = pty.fork()
                            if child == 0:
                                try:
                                    listener.close()
                                    connection.close()
                                    os.chdir(config["cwd"])
                                    os.execvpe(config["argv"][0], config["argv"], config["env"])
                                except BaseException as error:
                                    os.write(2, f"Session could not start: {error}\r\n".encode())
                                    os._exit(127)
                            attached = connection
                            resize(master, message.get("rows", 24), message.get("columns", 80))
                            continue
                        response = {"written": 0}
                        if "size" in message and master is not None:
                            resize(master, *message["size"])
                        elif "write" in message and master is not None and bracketed:
                            response["written"], input_generation = write_automatic(
                                master, attached, message, input_generation)
                        elif message.get("status"):
                            response = {"attached": attached is not None,
                                        "bracketed": bracketed, "child": child,
                                        "input_generation": input_generation}
                        connection.sendall(json.dumps(response).encode() + b"\n")
                    except (OSError, EOFError, ValueError, TypeError):
                        pass
                    finally:
                        if connection is not attached:
                            connection.close()
                elif source is attached:
                    incoming = attached.recv(65536)
                    if not incoming:
                        return
                    input_generation += 1
                    write_all(master, incoming)
                else:
                    try:
                        output = os.read(master, 65536)
                    except OSError:
                        return  # EIO is EOF for a closed PTY on some Unix hosts.
                    if not output:
                        return
                    combined = output_tail + output
                    # A PTY's line-discipline echo is NOT application receipt.
                    # Refuse injection until the application enables paste mode;
                    # the GUI separately confirms the visible Claude prompt.
                    enable = combined.rfind(b"\x1b[?2004h")
                    disable = combined.rfind(b"\x1b[?2004l")
                    if enable >= 0 or disable >= 0:
                        bracketed = enable > disable
                    output_tail = combined[-7:]
                    attached.sendall(output)
    finally:
        if attached is not None:
            attached.close()
        listener.close()
        if master is not None:
            os.close(master)
        try:
            _stop_child(child)
        finally:
            path.unlink(missing_ok=True)
            config_path.unlink(missing_ok=True)
            try:
                config_path.parent.rmdir()
            except OSError:
                pass


def attach(path):
    import termios
    import tty
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    previous = termios.tcgetattr(0)
    old_resize = None
    try:
        connection.connect(path)
        rows, columns = termios.tcgetwinsize(0)
        connection.sendall(json.dumps({"attach": True, "rows": rows,
                                       "columns": columns}).encode() + b"\n")
        tty.setraw(0)

        def resized(*_):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as control:
                control.settimeout(2)
                try:
                    control.connect(path)
                    size = termios.tcgetwinsize(0)
                    control.sendall(json.dumps({"size": size}).encode() + b"\n")
                    control.recv(1024)
                except OSError:
                    pass
        old_resize = signal.signal(signal.SIGWINCH, resized)
        while True:
            readable, _, _ = select.select([0, connection], [], [])
            for source in readable:
                if source == 0:
                    data = os.read(0, 65536)
                    if not data:
                        return
                    connection.sendall(data)
                else:
                    data = connection.recv(65536)
                    if not data:
                        return
                    write_all(1, data)
    finally:
        if old_resize is not None:
            signal.signal(signal.SIGWINCH, old_resize)
        termios.tcsetattr(0, termios.TCSADRAIN, previous)
        connection.close()


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("serve", "attach"):
        raise SystemExit("usage: _mac_relay.py serve CONFIG | attach SOCKET")
    try:
        (serve if sys.argv[1] == "serve" else attach)(sys.argv[2])
    except (OSError, EOFError) as error:
        raise SystemExit(f"Session relay closed: {error}")
