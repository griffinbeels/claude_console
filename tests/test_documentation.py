"""Guards on what this repository publishes, and on the split documentation.

This is a public repository, and it used to carry one machine's home directory
in four places — including the hook wiring and the consumer registry, where a
path is functional rather than decorative. Prose cannot hold that line.

The rest guard the split. CLAUDE.md is a map to `.claude/rules/`, and most of
those load only when a matching file is read, so a glob left behind by a rename
takes its whole rule out of every session with nothing to show for it.
"""
import json
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RULES = REPO / ".claude" / "rules"
ROOT_DOC = REPO / "CLAUDE.md"
SETTINGS = REPO / ".claude" / "settings.json"

ROOT_DOC_CEILING = 200
INVARIANTS = set(range(1, 16))

# This file is excluded from its own sweep: a scanner matching its own pattern
# is a scanner that can only ever fail.
_HOME_PATH = re.compile(
    r"(?:[A-Za-z]:[\\/]+Users[\\/]+|/home/|/Users/)(?!<)[A-Za-z0-9._-]+")


def _tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True,
        encoding="utf-8", check=True)
    return [line for line in result.stdout.splitlines() if line]


def test_no_tracked_file_carries_a_home_directory_path():
    offenders = []
    for relative in _tracked_files():
        if relative == f"tests/{Path(__file__).name}":
            continue
        try:
            text = (REPO / relative).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if _HOME_PATH.search(line):
                offenders.append(f"{relative}:{number}: {line.strip()}")

    assert not offenders, (
        "a home directory path is committed to a public repository:\n  "
        + "\n  ".join(offenders))


# Other projects on this machine. A repository that is not one of them has no
# reason to name one, and they arrive the same way every time: as the example in
# a test fixture or a comment, written while looking at whatever was on screen.
# `task_tracker` is deliberately absent — it is a declared consumer here.
FOREIGN_PROJECTS = ("sm64_tracker", "MARELO", "grime-to-five", "game-learnings")


def test_no_tracked_file_names_another_project_on_this_machine():
    offenders = []
    for relative in _tracked_files():
        if relative == f"tests/{Path(__file__).name}":
            continue
        try:
            text = (REPO / relative).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            for project in FOREIGN_PROJECTS:
                if project in line:
                    offenders.append(f"{relative}:{number}: {line.strip()[:90]}")

    assert not offenders, (
        "another project on this machine is named in a public repository — use "
        "a neutral example instead:\n  " + "\n  ".join(offenders))


def test_the_consumer_hook_points_at_a_script_that_exists():
    """The hook is what stops a change here breaking a consumer silently.

    It is wired with ${CLAUDE_PROJECT_DIR} so the path is not one machine's —
    measured to expand inside a hook's `args`, not just in `command`. A typo
    there disables the guard without any symptom at all: edits simply stop
    being checked.
    """
    settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    referenced = [
        argument
        for group in settings.get("hooks", {}).get("PostToolUse", [])
        for hook in group.get("hooks", [])
        for argument in hook.get("args", [])
    ]
    assert referenced, "no PostToolUse hook script is wired at all"

    for argument in referenced:
        assert "${CLAUDE_PROJECT_DIR}" in argument, (
            f"{argument!r} should be written relative to ${{CLAUDE_PROJECT_DIR}}")
        resolved = REPO / argument.replace("${CLAUDE_PROJECT_DIR}/", "")
        assert resolved.is_file(), f"the hook points at {resolved}, which is missing"


def test_the_consumer_registry_carries_no_absolute_paths():
    registry = json.loads((REPO / "consumers.json").read_text(encoding="utf-8"))
    for consumer in registry["consumers"]:
        assert not Path(consumer["path"]).is_absolute(), (
            f"{consumer['name']} is registered by absolute path; make it "
            "relative to this repo — tools/check_consumers.py resolves it")


def _rule_globs(rule: Path) -> list[str]:
    text = rule.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return []
    return re.findall(r'^\s*-\s*"([^"]+)"', text.split("---\n", 2)[1],
                      flags=re.MULTILINE)


def test_every_rule_glob_matches_a_file_that_exists():
    unmatched = [
        f"{rule.name}: {pattern!r} matches nothing"
        for rule in sorted(RULES.glob("*.md"))
        for pattern in _rule_globs(rule)
        if not list(REPO.glob(pattern))
    ]
    assert not unmatched, "\n  ".join(["stale globs:"] + unmatched)


def test_every_rule_file_is_named_in_the_root_map():
    root = ROOT_DOC.read_text(encoding="utf-8")
    on_disk = {rule.name for rule in RULES.glob("*.md")}
    listed = set(re.findall(r"\.claude/rules/([A-Za-z0-9._-]+\.md)", root))
    assert on_disk == listed, (
        f"on disk but unlisted: {sorted(on_disk - listed)}; "
        f"listed but missing: {sorted(listed - on_disk)}")


def test_every_invariant_number_lives_in_exactly_one_rule():
    heading = re.compile(r"^(\d+)\. \*\*", flags=re.MULTILINE)
    owners: dict[int, list[str]] = {}
    for rule in sorted(RULES.glob("*.md")):
        for number in heading.findall(rule.read_text(encoding="utf-8")):
            owners.setdefault(int(number), []).append(rule.name)

    duplicated = {n: files for n, files in owners.items() if len(files) > 1}
    assert not duplicated, f"claimed by more than one rule: {duplicated}"
    assert set(owners) == INVARIANTS, (
        f"missing: {sorted(INVARIANTS - set(owners))}; "
        f"unexpected: {sorted(set(owners) - INVARIANTS)}")


def test_the_root_map_stays_a_map():
    lines = ROOT_DOC.read_text(encoding="utf-8").splitlines()
    assert len(lines) <= ROOT_DOC_CEILING, (
        f"CLAUDE.md is {len(lines)} lines, over its {ROOT_DOC_CEILING}-line "
        "ceiling — it loads in every session in this repo. Move detail into a "
        "path-scoped rule under .claude/rules/ and link it from the table.")
