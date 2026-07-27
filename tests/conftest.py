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


@pytest.fixture(autouse=True)
def the_suite_never_writes_to_the_real_delivery_log(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONSOLE_LOG", str(tmp_path / "delivery.log"))
    return tmp_path / "delivery.log"
