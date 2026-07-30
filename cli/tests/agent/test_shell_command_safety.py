"""The allowlist that decides which shell commands need no approval.

This classifier is the only thing standing between "the gate stopped asking
about reading" and "the gate stopped asking". Its failure mode is silent — a
command wrongly called safe is one the owner is never shown — so the cases that
matter most here are the ones it must refuse.
"""

from __future__ import annotations

import pytest

from omni.skills_runtime.builtin_tools.shell import command_is_destructive, command_is_known_safe


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git log --oneline -30",
        "git diff HEAD~1",
        "git show a1b2c3d",
        "git log -p -1",
        "git diff -p",
        "git show -p HEAD",
        "git --no-pager log -1",
        "git log --grep='$HOME'",
        "git log --grep='feature*'",
        "git log --pretty='format:%h | %s' | head -250",
        "git log --grep='fix&&feat' | head -250",
        "git branch",
        "git branch -a",
        "pwd",
        # The shape the model actually emits: position, then look.
        "cd /Users/me/work/project && git log --oneline -30",
        'cd "/Users/me/my work/project" && git status',
        "git status; git log",
        "git log | git diff",
        "git show HEAD | head",
        "git show HEAD | head -250",
        "git show HEAD | head -n 250",
        "git show HEAD | head -n250",
        "git show HEAD | head -c 1024",
        "git show HEAD | head -c1024",
        "git show HEAD | head --lines=40",
        "git show HEAD | head --bytes=1024",
        "git show HEAD |\n head -250",
        (
            "cd /Users/antonio/work/omniscientist_v2 && "
            "git show e16c8e0d -p -- "
            "cli/src/omni/cli/commands/tasks_cmd.py "
            "cli/src/omni/runtime/task_recorder.py | head -250"
        ),
        # The bash tool already merges stderr; a trailing merge is a no-op.
        "git show 4c4ea93e -- cli/src/omni/scheduling/temporal.py 2>&1",
        "git log --oneline -30 2>&1",
        "git show HEAD 2>&1 | head -250",
        "cd /tmp/project && git status 2>&1",
    ],
)
def test_reporting_on_the_tree_needs_no_permission(command: str) -> None:
    assert command_is_known_safe(command) is True


@pytest.mark.parametrize(
    ("command", "why"),
    [
        ("git log > out.txt", "redirection writes a file"),
        ("git log > out.txt 2>&1", "stdout redirect is still a write after stripping stderr merge"),
        ("git log >> out.txt", "appending writes a file"),
        ("git status < /etc/passwd", "redirection reads a file of its choosing"),
        ("git log && rm -rf build", "one unsafe segment taints the composite"),
        ("rm -rf build && git log", "order does not launder the unsafe segment"),
        ("./git log", "path-qualified argv[0] is a different binary"),
        ("/usr/bin/git log", "path-qualified argv[0] is a different binary"),
        ("git -c core.pager=sh log", "-c redirects where git reads config"),
        ("git -C /elsewhere log", "-C redirects which repository is read"),
        ("git --git-dir=/elsewhere/.git log", "redirects which repository is read"),
        ("git --paginate log -1", "a global pager may execute an external helper"),
        ("git --help log", "the global help flag may launch an external viewer"),
        ("git -p log -1", "the global short pager flag may execute an external helper"),
        ("git --super-prefix=elsewhere log", "changes git's global execution context"),
        ("git log -1 --output=/tmp/log", "writes command output to a file"),
        ("git diff --output /tmp/diff", "writes command output to a file"),
        ("git show HEAD --output=/tmp/show", "writes command output to a file"),
        ("git diff --no-index ~/.ssh/id_rsa /dev/null", "reads arbitrary filesystem paths"),
        ("git diff --ext-diff HEAD", "may execute an external diff helper"),
        ("git log --textconv -1", "may execute a configured textconv helper"),
        ("git log --exec=helper -1", "may execute an external helper"),
        ("git diff -O /tmp/order", "reads an arbitrary order file"),
        ("git diff --order-file=/tmp/order", "reads an arbitrary order file"),
        ("git status --pathspec-from-file=/tmp/paths", "reads an arbitrary pathspec file"),
        ("git log --show-signature -1", "may execute a configured GPG helper"),
        ("git log --help", "may launch an external help viewer"),
        ("git log $EVIL", "shell expansion can inject unchecked arguments"),
        ('git log "$REV"', "double-quoted shell expansion still changes argv"),
        ("git diff *", "pathname expansion can inject unchecked arguments"),
        ("git branch --list $PATTERN", "shell expansion bypasses branch argv review"),
        ("git push", "publishes"),
        ("git commit -m x", "writes"),
        ("git branch -m old new", "renames a branch"),
        ("git branch --color surprise", "creates a branch; --color has only an optional inline value"),
        ("git branch --column surprise", "creates a branch; --column has only an optional inline value"),
        ("git branch --merged HEAD", "complex branch queries use ordinary approval"),
        ("git branch --no-merged HEAD", "complex branch queries use ordinary approval"),
        ("git branch -r origin/main", "positional branch filters use ordinary approval"),
        ("git --no-pager branch --list feature", "Codex keeps positional branch patterns behind approval"),
        ("git --no-pager branch -D old", "global flags must not hide deletion"),
        ("git reset --hard", "rewrites the tree"),
        ("echo $(cat ~/.ssh/id_rsa)", "command substitution"),
        ("echo `cat ~/.ssh/id_rsa`", "command substitution"),
        ("git log & sleep 5", "backgrounding"),
        ("head -250", "an unpiped head could consume the interactive terminal"),
        ("git show HEAD && head -250", "head must consume a pipe, not the terminal"),
        ("head README.md", "head may not read a named file without approval"),
        ("head -n 250 /etc/passwd", "a count option does not make a file operand safe"),
        ("git show HEAD | head -n +250", "a positive offset reads through end of input"),
        ("git show HEAD | head -n -250", "a negative count reads through end of input"),
        ("head -- ~/.ssh/id_rsa", "an option terminator still exposes a file operand"),
        ("head -q .env", "formatting flags must not expose a file operand"),
        ("head -250foo", "a legacy count must be entirely numeric"),
        (
            "git show HEAD | head -n 250 /etc/passwd",
            "a safe producer does not make a file-reading consumer safe",
        ),
        ("git show HEAD | head -250 > out.txt", "redirection still writes a file"),
        ("git show HEAD | head -250 | sh", "an executing consumer taints the pipeline"),
        (
            "git show HEAD # |\nhead -250",
            "a pipe in a comment is not stdin for the following command",
        ),
        ("cat README.md", "reads a file of its choosing"),
        ("ls /etc", "not on the list"),
        (
            "command -v dot && dot -Tpng figure.dot -o figure.png",
            "dot writes files and is not on Codex's known-safe list",
        ),
        ("git", "no subcommand"),
        ("", "nothing to classify"),
        ("   ", "nothing to classify"),
        ("cd /tmp &&", "an empty segment is not a safe segment"),
        ('git log "unclosed', "unparseable is not provably safe"),
    ],
)
def test_anything_else_still_reaches_the_owner(command: str, why: str) -> None:
    assert command_is_known_safe(command) is False, why


@pytest.mark.parametrize(
    "command",
    [
        "omni task rm deadbeef --force",
        "omni -P project task rm deadbeef --force",
        "omni -Pproject task rm deadbeef --force",
        "/opt/bin/omni --project project task delete deadbeef",
        " omni --project=project task rm deadbeef --force",
        "python -m omni.cli.main task rm deadbeef --force",
        "FOO=bar omni task rm deadbeef --force",
        "command omni task rm deadbeef --force",
        "uv run omni task rm deadbeef --force",
        "nohup uv run omni task rm deadbeef --force",
        "git branch -D old",
        "git branch --delete old",
        "git --no-pager branch -D old",
        "git -C /tmp/repo branch -m old new",
        "git branch new-name",
        "git branch --color surprise",
        "git branch --column surprise",
        "git branch --no-color surprise-2",
        "git branch -v surprise-3",
        "git branch --verbose surprise-4",
        "git branch --ignore-case surprise-5",
        "git branch --no-column surprise-6",
        "env git branch -D old",
        "env FOO=bar git branch --delete old",
        "find . -delete",
    ],
)
def test_destructive_command_variants_are_classified_as_destructive(command: str) -> None:
    assert command_is_destructive(command) is True


def test_branch_query_without_a_positional_is_not_mislabeled_destructive() -> None:
    assert command_is_destructive("git branch --unknown-option") is False
