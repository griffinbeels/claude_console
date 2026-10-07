"""Guards that apply to every test in this repo.

There is one, and it exists because the delivery path now writes a log. That
log's default home is the user's own `%LOCALAPPDATA%\\claude_console`, and its
whole value is being readable after a hand-off went wrong — a suite that
appends its own fixtures to it would bury the one occurrence somebody needs.

Redirecting it here rather than in each test file is the point: a new test
file that exercises `deliver` inherits the redirect instead of having to
remember it, and remembering is what fails.
"""

import pytest
import sys


@pytest.fixture(autouse=True)
def shell_profiles_are_fixtures_on_mac(tmp_path, monkeypatch):
    if sys.platform == "darwin":
        import pwd
        from types import SimpleNamespace
        account = SimpleNamespace(pw_dir=str(tmp_path), pw_name="console-test",
                                  pw_shell="/bin/zsh")
        monkeypatch.setattr(pwd, "getpwuid", lambda uid: account)


@pytest.fixture(autouse=True)
def no_test_opens_or_controls_terminal(monkeypatch):
    from claude_console import macos
    def forbidden(*args, **kwargs):
        pytest.fail("Tests must replace Terminal automation; no window or user tab may be touched")
    monkeypatch.setattr(macos, "_applescript", forbidden)


@pytest.fixture(autouse=True)
def the_suite_never_writes_to_the_real_delivery_log(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONSOLE_LOG", str(tmp_path / "delivery.log"))
    return tmp_path / "delivery.log"
