"""Shell tool with a two-tier sandbox guard.

Commands run in the tool *working directory* (the folder the CLI was launched
from) with a timeout. ``security.bash_sandbox`` selects the tier:

* ``readonly`` — blocks both the system tier and the workspace-destructive tier
  (delete/rewrite/publish); intended for "just look" runs.
* ``workspace-write`` (default for interactive CLI) — allows destructive ops
  *inside* the working directory (``rm -rf``, ``git reset --hard``, ``git push``)
  but still routes them through the approval gate; system-tier ops stay blocked.
* ``full`` — removes the guard entirely (system tier included).

The system tier (``sudo``, ``mkfs``, ``dd if=``, fork bomb, ``>/dev/sd``,
``shutdown``/``reboot``, recursive ``chown``/``chmod`` on ``/``, ``curl | sh``)
escapes the working directory, so no approval prompt can widen scope to it — it
is hard-blocked unless the sandbox is explicitly ``full``. This is a guard, not
a jail — for a hard sandbox use the OS (containers, seatbelt).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from omni.channels.security import channel_requires_sensitive_confirm
from omni.core.react_agent import ToolSpec
from omni.core.tool_result import (
    COMMAND_RESULT_SCHEMA,
    ToolResultEnvelope,
    command_exit_summary,
    command_output_window,
)
from omni.runtime.processes import process_group_options, stop_process_tree
from omni.skills_runtime.context import ExecContext, Tool
from omni.skills_runtime.exec_io import (
    OMNI_OUTPUT_ENV,
    compute_env,
    durable_output_dir,
    exec_tmp_dir,
    register_output_dir,
)
from omni.skills_runtime.manifest import python_module_available
from omni.skills_runtime.sandbox import SandboxUnavailableError

_EVENT_OUTPUT_JSON_BUDGET = 7_000
_OBSERVATION_OUTPUT_LIMIT = 100_000

# System / irreversible-beyond-workspace operations. Hard-blocked in every tier
# except ``full`` because their blast radius escapes the working directory, so
# no in-directory approval could make them safe.
_BLOCKED = [
    r"\bsudo\b", r"\bmkfs\b", r"\bdd\s+if=", r":\(\)\s*\{",
    r">\s*/dev/sd", r"\bshutdown\b", r"\breboot\b",
    r"\bchown\s+-R\s+/", r"\bchmod\s+-R\s+777\s+/",
    r"\bcurl\b[^|]*\|\s*(sudo\s+)?(ba)?sh",
]
# Destructive within the working directory (delete/rewrite/publish). Blocked in
# ``readonly``; allowed in ``workspace-write``/``full`` but still routed through
# the approval gate (classified ``destructive`` for an explicit high-risk prompt).
_DESTRUCTIVE_WORKSPACE = [
    r"\brm\s+-rf?\b",
    r"\bgit\s+push\b",
    r"\bgit\s+reset\s+--hard\b",
    r"\bfind\b[^\n;|&]*\s-delete\b",
]
_BLOCKED_RE = re.compile("|".join(_BLOCKED), re.IGNORECASE)
# Union: used only to classify approval risk (either tier is "destructive").
_DESTRUCTIVE_RE = re.compile("|".join(_BLOCKED + _DESTRUCTIVE_WORKSPACE), re.IGNORECASE)

# Operators that join commands without adding an effect of their own. ``||``
# precedes ``|`` so the two-character form wins the alternation.
_SHELL_SEGMENTS = re.compile(r"\|\||&&|;|\||\n")
_HEAD_COUNT = re.compile(r"[0-9]+")
_HEAD_LEGACY_COUNT = re.compile(r"-[0-9]+")
_HEAD_ATTACHED_COUNT = re.compile(r"-[nc][0-9]+")
_HEAD_LONG_COUNT = re.compile(r"--(?:lines|bytes)=[0-9]+")
_TRAILING_STDERR_MERGE = re.compile(r"[ \t]+2>&1$")


def posix_shell_executable() -> str | None:
    """POSIX shell that understands ``$VAR`` and ``test -f``.

    The bash tool's contract is Bourne syntax. ``create_subprocess_shell`` on
    Windows is cmd.exe, which does not expand ``$OMNI_OUTPUT_DIR``. Prefer Git
    Bash there; skip the WSL ``System32\\bash.exe`` launcher because it sees a
    different filesystem.
    """
    if os.name != "nt":
        return "/bin/sh"
    for candidate in _windows_posix_shells():
        if candidate.is_file():
            return str(candidate)
    return None


def _windows_posix_shells() -> list[Path]:
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    found = [
        Path(pf) / "Git" / "bin" / "bash.exe",
        Path(pf86) / "Git" / "bin" / "bash.exe",
    ]
    if local:
        found.append(Path(local) / "Programs" / "Git" / "bin" / "bash.exe")
    which = shutil.which("bash")
    if which:
        path = Path(which)
        if path.parent.name.lower() != "system32":
            found.append(path)
    return found


async def spawn_user_shell(
    command: str,
    *,
    prefix: Sequence[str],
    spawn: Mapping[str, Any],
) -> asyncio.subprocess.Process:
    """Run *command* under a POSIX shell when one exists, else the host shell."""
    options = dict(spawn)
    if prefix:
        return await asyncio.create_subprocess_exec(
            *prefix, "/bin/sh", "-c", command, **options
        )
    shell = posix_shell_executable()
    if shell is not None:
        return await asyncio.create_subprocess_exec(shell, "-c", command, **options)
    return await asyncio.create_subprocess_shell(command, **options)


_KNOWN_SAFE_GIT_SUBCOMMANDS = frozenset({"status", "log", "diff", "show"})
# Global options that move where git reads its config, repository, or helpers
# from — the point at which a read-only subcommand stops being read-only.
_GIT_UNSAFE_GLOBAL_EXACT = frozenset(
    {
        "-c",
        "-C",
        "-p",
        "--config-env",
        "--exec-path",
        "--git-dir",
        "--help",
        "--namespace",
        "--paginate",
        "--super-prefix",
        "--work-tree",
    }
)
_GIT_UNSAFE_GLOBAL_PREFIXES = (
    "--config-env=",
    "--exec-path=",
    "--git-dir=",
    "--namespace=",
    "--super-prefix=",
    "--work-tree=",
)
_GIT_GLOBAL_VALUE_OPTIONS = frozenset(
    {
        "-c",
        "-C",
        "--config-env",
        "--exec-path",
        "--git-dir",
        "--namespace",
        "--super-prefix",
        "--work-tree",
    }
)
_GIT_UNSAFE_SUBCOMMAND_EXACT = frozenset(
    {
        "-O",
        "--exec",
        "--ext-diff",
        "--help",
        "--no-index",
        "--order-file",
        "--output",
        "--pathspec-file-nul",
        "--pathspec-from-file",
        "--show-signature",
        "--textconv",
    }
)
_GIT_UNSAFE_SUBCOMMAND_PREFIXES = (
    "-O",
    "--exec=",
    "--order-file=",
    "--output=",
    "--pathspec-from-file=",
)
_GIT_BRANCH_READ_FLAGS = frozenset(
    {
        "-a",
        "--all",
        "-r",
        "--remotes",
        "--list",
        "--show-current",
        "-v",
        "-vv",
        "--verbose",
        "--ignore-case",
        "--no-color",
        "--no-column",
    }
)
_GIT_BRANCH_MUTATION_OPTIONS = frozenset(
    {
        "-c",
        "-C",
        "-d",
        "-D",
        "-m",
        "-M",
        "-u",
        "--copy",
        "--create-reflog",
        "--delete",
        "--edit-description",
        "--move",
        "--no-track",
        "--recurse-submodules",
        "--set-upstream",
        "--set-upstream-to",
        "--track",
        "--unset-upstream",
    }
)
_GIT_BRANCH_QUERY_OPTIONS = frozenset(
    {
        "--list",
        "-a",
        "--all",
        "-r",
        "--remotes",
        "--contains",
        "--merged",
        "--no-contains",
        "--no-merged",
        "--points-at",
    }
)
_GIT_BRANCH_QUERY_VALUE_OPTIONS = frozenset(
    {"--contains", "--merged", "--no-contains", "--no-merged", "--points-at"}
)
_OMNI_TASK_DELETE_VERBS = frozenset({"rm", "remove", "delete", "clear", "prune"})
_OMNI_GLOBAL_VALUE_OPTIONS = frozenset(
    {"-P", "--project", "--profile", "--model", "-m", "--ui", "--out"}
)
_OMNI_GLOBAL_FLAG_OPTIONS = frozenset(
    {"--continue", "-c", "--trust", "--no-trust", "--debug", "--version", "-V"}
)
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _fits_event_output_budget(value: dict) -> bool:
    encoded = json.dumps(value, default=str, ensure_ascii=False)
    return (
        len(encoded) <= _EVENT_OUTPUT_JSON_BUDGET
        and len(encoded.encode("utf-8")) <= _EVENT_OUTPUT_JSON_BUDGET
    )


def _fit_event_field(
    result: dict,
    field: str,
    text: str,
    *,
    prefer_tail: bool,
) -> None:
    """Shrink ``result[field]`` until the serialized object fits the budget."""
    if prefer_tail:
        low, high = 0, len(text)
        best = ""
        while low <= high:
            midpoint = (low + high + 1) // 2
            result[field] = (
                text if midpoint >= len(text) else command_output_window(text, midpoint)
            )
            if _fits_event_output_budget(result):
                best = result[field]
                low = midpoint + 1
            else:
                high = midpoint - 1
        result[field] = best
        return
    low, high = 0, len(text)
    while low < high:
        midpoint = (low + high + 1) // 2
        result[field] = text[:midpoint]
        if _fits_event_output_budget(result):
            low = midpoint
        else:
            high = midpoint - 1
    result[field] = text[:low]


def _bounded_event_output(
    *,
    command_status: str,
    reason: str,
    exit_code: int | None,
    output: str,
    summary: str,
    stderr: str = "",
) -> dict:
    """Build a command result that survives the recorder's whole-value limit.

    The recorder currently replaces a complete JSON value once its serialized
    form exceeds 8,000 Python characters. A raw character slice is insufficient:
    JSON escaping can multiply quotes, control characters, and backslashes, while
    non-ASCII code points can use several UTF-8 bytes. Budget the complete object
    with headroom. Failed commands keep the tail (and stderr) so a progress
    dump cannot hide ``Permission denied``.
    """
    failed = command_status == "failed"
    stderr = str(stderr or "")
    result = {
        "result_schema": COMMAND_RESULT_SCHEMA,
        "command_status": command_status,
        "reason": reason,
        "exit_code": exit_code,
        "output": output,
        "output_truncated": False,
        "summary": summary,
    }
    if stderr:
        result["stderr"] = stderr
    if _fits_event_output_budget(result):
        return result

    result["output_truncated"] = True
    if stderr:
        result["stderr"] = ""
        result["output"] = ""
        _fit_event_field(result, "stderr", stderr, prefer_tail=failed)
        if not result["stderr"]:
            result.pop("stderr", None)
    _fit_event_field(result, "output", output, prefer_tail=failed)
    return result


def _command_result(
    observation: str,
    *,
    command_status: str,
    reason: str,
    exit_code: int | None,
    output: str,
    summary: str,
    stderr: str = "",
) -> ToolResultEnvelope:
    return ToolResultEnvelope(
        observation=observation,
        event_output=_bounded_event_output(
            command_status=command_status,
            reason=reason,
            exit_code=exit_code,
            output=output,
            summary=summary,
            stderr=stderr,
        ),
    )


def _controlled_result(
    observation: str,
    *,
    command_status: str,
    reason: str,
    summary: str,
) -> ToolResultEnvelope:
    return _command_result(
        observation,
        command_status=command_status,
        reason=reason,
        exit_code=None,
        output=observation,
        summary=summary,
    )


def command_is_system_blocked(command: str) -> bool:
    """True for system/irreversible ops that escape the working directory.

    Hard-blocked in every sandbox tier except ``full`` — no in-directory
    approval can widen scope this far.
    """
    return bool(_BLOCKED_RE.search(command or ""))


def command_is_destructive(command: str) -> bool:
    """True if ``command`` matches any destructive/privileged pattern.

    Union of the system-block tier and the workspace-destructive tier so the
    approval gate classifies either as ``risk='destructive'`` so the prompt can
    state the risk plainly, independent of the active sandbox tier.
    """
    text = command or ""
    return bool(
        _DESTRUCTIVE_RE.search(text)
        or _omni_task_is_destructive(text)
        or _git_branch_is_destructive(text)
    )


def _omni_task_is_destructive(command: str) -> bool:
    """Recognize task deletion through the installed or Python Omni entrypoint."""
    for segment in _SHELL_SEGMENTS.split(str(command or "").strip()):
        try:
            words = shlex.split(segment)
        except ValueError:
            continue
        words = _unwrap_simple_command(words)
        if not words:
            continue
        executable = words[0].rsplit("/", 1)[-1].lower()
        if executable == "omni":
            args = words[1:]
        elif (
            re.fullmatch(r"python(?:[0-9.]+)?", executable)
            and len(words) >= 4
            and words[1:3] == ["-m", "omni.cli.main"]
        ):
            args = words[3:]
        else:
            continue
        index = 0
        while index < len(args):
            token = args[index]
            if token in _OMNI_GLOBAL_VALUE_OPTIONS:
                index += 2
                continue
            if token.startswith("-P") and token != "-P":
                index += 1
                continue
            if any(token.startswith(f"{option}=") for option in _OMNI_GLOBAL_VALUE_OPTIONS):
                index += 1
                continue
            if token in _OMNI_GLOBAL_FLAG_OPTIONS:
                index += 1
                continue
            break
        if (
            index + 1 < len(args)
            and args[index].lower() == "task"
            and args[index + 1].lower() in _OMNI_TASK_DELETE_VERBS
        ):
            return True
    return False


def _unwrap_simple_command(words: list[str]) -> list[str]:
    """Expose Omni behind common non-shell execution wrappers.

    Approval-rule validation rejects wrappers outright. This narrow unwrapping
    exists only so the risk label remains truthful for an Omni delete hidden
    behind an assignment, ``env``, ``command``, or ``uv run``.
    """
    index = 0
    while index < len(words):
        while index < len(words) and _ENV_ASSIGNMENT.match(words[index]):
            index += 1
        if index >= len(words):
            break
        executable = words[index].rsplit("/", 1)[-1].lower()
        if executable in {"command", "exec", "nohup"}:
            index += 1
            continue
        if executable == "env":
            index += 1
            while index < len(words):
                token = words[index]
                if token == "--":
                    index += 1
                    break
                if _ENV_ASSIGNMENT.match(token):
                    index += 1
                    continue
                if token in {"-u", "--unset", "-C", "--chdir"}:
                    index += 2
                    continue
                if token.startswith(("--unset=", "--chdir=")):
                    index += 1
                    continue
                if token in {"-i", "--ignore-environment", "-0", "--null"}:
                    index += 1
                    continue
                # Unknown env options may consume values or split a command.
                # Keep the wrapper opaque rather than guessing past them.
                if token.startswith("-"):
                    return words
                break
            continue
        if executable == "uv" and words[index + 1 : index + 2] == ["run"]:
            index += 2
            continue
        break
    return words[index:]


def _git_branch_is_destructive(command: str) -> bool:
    """Deny-first only branch forms that explicitly mutate repository state."""
    for segment in _SHELL_SEGMENTS.split(str(command or "").strip()):
        try:
            words = _unwrap_simple_command(shlex.split(segment))
        except ValueError:
            continue
        if not words or words[0].rsplit("/", 1)[-1].lower() != "git":
            continue
        subcommand, args, _redirected = _split_git_invocation(words[1:])
        if subcommand == "branch" and _git_branch_args_are_destructive(args):
            return True
    return False


def _git_branch_args_are_destructive(args: list[str]) -> bool:
    query_mode = False
    index = 0
    while index < len(args):
        token = args[index]
        option = token.split("=", 1)[0]
        if option in _GIT_BRANCH_MUTATION_OPTIONS:
            return True
        if option in _GIT_BRANCH_QUERY_OPTIONS:
            query_mode = True
            if option in _GIT_BRANCH_QUERY_VALUE_OPTIONS and "=" not in token:
                index += 1
            index += 1
            continue
        if token.startswith("-"):
            # Presentation flags do not consume a following positional unless
            # their optional value is inline. Continue so a later branch name
            # is still classified as a create.
            index += 1
            continue
        if query_mode:
            return False
        return True
    return False


def _git_global_option_is_unsafe(token: str) -> bool:
    if token in _GIT_UNSAFE_GLOBAL_EXACT:
        return True
    if token.startswith(_GIT_UNSAFE_GLOBAL_PREFIXES):
        return True
    return any(token.startswith(option) and len(token) > len(option) for option in ("-c", "-C"))


def _git_subcommand_args_are_read_only(args: list[str]) -> bool:
    """Reject arguments that can write, execute helpers, or read arbitrary files."""
    for token in args:
        if token in _GIT_UNSAFE_SUBCOMMAND_EXACT:
            return False
        if token.startswith(_GIT_UNSAFE_SUBCOMMAND_PREFIXES):
            return False
    return True


def _split_git_invocation(args: list[str]) -> tuple[str, list[str], bool]:
    """Return subcommand, its args, and whether a global option is unsafe."""
    unsafe_global = False
    index = 0
    while index < len(args):
        token = args[index]
        option = token.split("=", 1)[0]
        unsafe_global = unsafe_global or _git_global_option_is_unsafe(token)
        if option in _GIT_GLOBAL_VALUE_OPTIONS and "=" not in token:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token, args[index + 1 :], unsafe_global
    return "", [], unsafe_global


def _git_branch_is_read_only(args: list[str]) -> bool:
    """Mirror Codex's narrow, argument-local branch-listing grammar."""
    if not args:
        return True
    saw_read_flag = False
    for token in args:
        if token in _GIT_BRANCH_READ_FLAGS:
            saw_read_flag = True
        elif token.startswith("--format="):
            saw_read_flag = True
        else:
            return False
    return saw_read_flag


def command_is_known_safe(command: str) -> bool:
    """True when a command's whole effect is to report on the working tree.

    The approval gate could previously tell a *shell call* from a file write but
    not one command from another, so ``git log`` was interrogated exactly as
    hard as ``rm -rf``. A prompt that fires for reading gets dismissed by habit,
    and it is the same prompt that has to stop a delete.

    Modelled on Codex's ``is_known_safe_command``: a composite is safe only when
    every segment is, and ``argv[0]`` must be a bare word — a path-qualified
    ``./git`` is whatever binary sits at that path and merely shares a name.
    The list is deliberately short. A name earns its place by being unable to
    write, publish, or read a file of the caller's choosing, and it should grow
    one reviewed entry at a time rather than by analogy.
    """
    text = str(command or "").strip()
    if not text or command_is_destructive(text) or command_is_system_blocked(text):
        return False
    segments = _split_safe_shell_segments(text)
    if segments is None:
        return False
    return all(
        _segment_is_known_safe(segment, stdin_from_pipe=operator == "|")
        for segment, operator in segments
    )


def _split_safe_shell_segments(text: str) -> list[tuple[str, str | None]] | None:
    """Return simple command segments while keeping each preceding operator."""
    segments: list[tuple[str, str | None]] = []
    preceding_operator: str | None = None
    quote = ""
    escaped = False
    start = index = 0
    while index < len(text):
        char = text[index]
        if escaped:
            if char == "\n":
                return None
            escaped = False
        elif quote:
            if char == quote:
                quote = ""
            elif quote == '"' and char == "\\":
                escaped = True
        elif char == "\\":
            escaped = True
        elif char in {"'", '"'}:
            quote = char
        elif char == "#":
            # A pipe written after a shell comment is not stdin for the next
            # line. Comments are uncommon in generated calls, so fail closed
            # instead of growing this safety parser into a shell interpreter.
            return None
        else:
            operator = (
                text[index : index + 2]
                if text[index : index + 2] in {"||", "&&"}
                else char if char in ";|\n" else ""
            )
            if operator:
                segment = text[start:index].strip()
                if not segment:
                    if operator == "\n" and segments:
                        start = index + 1
                        index += 1
                        continue
                    return None
                segments.append((segment, preceding_operator))
                preceding_operator = operator
                index += len(operator)
                start = index
                continue
        index += 1

    segment = text[start:].strip()
    if quote or escaped or not segment:
        return None
    segments.append((segment, preceding_operator))
    return segments


def _strip_trailing_stderr_merge(text: str) -> str:
    """Drop a trailing stderr-merge that this tool already performs."""
    return _TRAILING_STDERR_MERGE.sub("", str(text or "").rstrip())


def _segment_is_known_safe(segment: str, *, stdin_from_pipe: bool = False) -> bool:
    # The bash tool already merges stderr into stdout, so a trailing ``2>&1``
    # is a no-op here. Strip it before looking for shell effects, otherwise a
    # read-only ``git show … 2>&1`` is interrogated as a redirect. A real
    # stdout redirect (``> file 2>&1``) still contains ``>`` after the strip.
    segment = _strip_trailing_stderr_merge(segment)
    if _has_shell_expansion_or_effect(segment):
        return False
    try:
        words = shlex.split(segment)
    except ValueError:
        return False
    if not words or "/" in words[0] or words[0].startswith("-"):
        return False
    verb, args = words[0], words[1:]
    if verb == "cd":
        # Positioning, not access: it reads and writes nothing by itself, and
        # whatever runs after it is held to this same list.
        return True
    if verb == "pwd":
        return True
    if verb == "head":
        return stdin_from_pipe and _head_reads_only_stdin(args)
    if verb == "git":
        return _git_is_known_safe(args)
    return False


def _head_reads_only_stdin(args: list[str]) -> bool:
    """Accept bounded ``head`` forms only when no file operand is present."""
    if not args:
        return True
    if len(args) == 1:
        token = args[0]
        return bool(
            _HEAD_LEGACY_COUNT.fullmatch(token)
            or _HEAD_ATTACHED_COUNT.fullmatch(token)
            or _HEAD_LONG_COUNT.fullmatch(token)
        )
    return bool(
        len(args) == 2
        and args[0] in {"-n", "--lines", "-c", "--bytes"}
        and _HEAD_COUNT.fullmatch(args[1])
    )


def _has_shell_expansion_or_effect(text: str) -> bool:
    """Detect syntax whose runtime argv/effect differs from the reviewed text."""
    quote = ""
    escaped = False
    for char in text:
        if escaped:
            escaped = False
            continue
        if quote == "'":
            if char == "'":
                quote = ""
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "'" and not quote:
            quote = char
            continue
        if char == '"':
            quote = "" if quote == char else char
            continue
        if char in "$`":
            return True
        if not quote and char in "<>&*?[{":
            return True
    return False


def _git_is_known_safe(args: list[str]) -> bool:
    """True for a read-only Git invocation, including its complete argv."""
    subcommand, subcommand_args, unsafe_global = _split_git_invocation(args)
    if unsafe_global or not _git_subcommand_args_are_read_only(subcommand_args):
        return False
    if subcommand == "branch":
        return _git_branch_is_read_only(subcommand_args)
    return subcommand in _KNOWN_SAFE_GIT_SUBCOMMANDS


def command_writes_git_metadata(command: str) -> bool:
    """True when a git command must write ``.git`` (add / commit / …).

    Codex keeps ``.git`` read-only under WorkspaceWrite unless an explicit
    write rule is granted. Approving a mutating git command is that grant:
    the host adds ``cwd/.git`` for this spawn only, never the Omni store.
    """
    if command_is_known_safe(command):
        return False
    try:
        words = shlex.split(str(command or "").strip())
    except ValueError:
        return False
    return bool(words) and words[0] == "git"


def git_metadata_write_roots(command: str, cwd: Path | None) -> list[Path]:
    """Precise additional writable roots for an approved mutating git command."""
    if cwd is None or not command_writes_git_metadata(command):
        return []
    return [Path(cwd) / ".git"]


# Package-install verbs whose *ad-hoc* use inside a ReAct turn targets the wrong
# interpreter/venv (PEP 668, missing ``.venv/bin/pip``, ``--break-system-packages``)
# — the exact loop that degraded a "read the deck I just made" task. We only
# *intercept* an install whose target omni already provides or a capability
# declares; a genuinely new dependency still runs. This is the shell-side twin of
# the ``open_artifact`` / executor preflights: capability acquisition is declared,
# never hand-rolled mid-loop.
_INSTALL_VERB_RE = re.compile(
    r"(?i)\b("
    r"pip[0-9]*\s+install"
    r"|python[0-9.]*\s+-m\s+pip\s+install"
    r"|uv\s+pip\s+install"
    r"|uv\s+add"
    r"|pipx\s+install"
    r"|poetry\s+add"
    r"|(?:conda|mamba)\s+install"
    r"|easy_install"
    r")\b"
)
# Flags that consume the following token (so it is never mistaken for a package).
_VALUE_FLAGS = frozenset(
    {
        "-r", "--requirement", "-c", "--constraint", "-i", "--index-url",
        "--extra-index-url", "-f", "--find-links", "--python", "-p",
        "--target", "-t", "--prefix", "--root",
    }
)


def _dist_name(token: str) -> str:
    """Leading PEP 508 distribution name from a requirement token
    (``python-pptx==0.6.21`` → ``python-pptx``; ``pptx[foo]`` → ``pptx``)."""
    stripped = token.strip().strip("'\"")
    match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", stripped)
    return match.group(0) if match else ""


def _install_targets(command: str) -> list[str]:
    """Named package targets of any install verb in ``command`` (best-effort).

    Splits on shell separators so ``a && pip install pptx`` is seen; skips flags,
    option values, requirement/constraint files, and local paths / URLs / VCS
    specs (which we cannot resolve to a distribution name and must not block).
    """
    targets: list[str] = []
    for segment in re.split(r"&&|\|\||[;|\n]", command):
        verb = _INSTALL_VERB_RE.search(segment)
        if verb is None:
            continue
        try:
            tokens = shlex.split(segment[verb.end():])
        except ValueError:
            tokens = segment[verb.end():].split()
        skip_next = False
        for tok in tokens:
            if skip_next:
                skip_next = False
                continue
            if tok in _VALUE_FLAGS:
                skip_next = True
                continue
            if tok.startswith("-"):
                continue
            if (
                tok in {".", ".."}
                or "/" in tok
                or "://" in tok
                or tok.endswith((".whl", ".tar.gz", ".tgz", ".zip", ".git"))
            ):
                continue
            name = _dist_name(tok)
            if name:
                targets.append(name)
    return targets


def _already_available(name: str) -> bool:
    """True if ``name`` is importable / installed in omni's own interpreter.

    Delegates to the single availability oracle shared with the admission gate
    (:func:`omni.skills_runtime.manifest.python_module_available`) so an install
    that would only re-fetch what omni already has is recognised as redundant the
    exact same way admission decides a declared module is present.
    """
    return python_module_available(name)


def _dependency_install_interception(command: str, ctx: ExecContext) -> ToolResultEnvelope | None:
    """Intercept an ad-hoc install of a package omni already provides or a
    capability declares — return routing guidance instead of running it.

    Returns ``None`` (command proceeds) for a genuinely new dependency, a
    requirement file, or a local/VCS install, so this never blocks legitimate
    setup — it only stops the doomed cross-interpreter re-install loop.
    """
    targets = _install_targets(command)
    if not targets:
        return None
    registry = getattr(ctx, "registry", None)
    provided: list[str] = []
    provider_notes: list[str] = []
    seen_providers: set[str] = set()
    for name in targets:
        provider = None
        if registry is not None:
            variants = {name, name.replace("-", "_"), name.replace("_", "-")}
            provider = registry.find_python_module_provider(variants)
        available = _already_available(name)
        if provider is None and not available:
            # A genuinely new dependency in the command. Interception is a guard
            # against re-fetching what omni already has — never a blocker on real
            # setup — so a *mixed* install (new + redundant) must proceed intact
            # rather than being rejected wholesale for the redundant token.
            return None
        provided.append(name)
        if provider is not None and provider.name not in seen_providers:
            seen_providers.add(provider.name)
            setup = str(getattr(provider, "dependency_setup_command", "") or "").strip()
            note = f"'{name}' is owned by the '{provider.name}' capability"
            if setup:
                note += f" (owner setup: {setup})"
            provider_notes.append(note)
    if not provided:
        return None
    joined = ", ".join(dict.fromkeys(provided))
    lines = [
        f"BLOCKED ad-hoc install: {joined} — already available to omni "
        "(a mid-loop pip/uv install here targets the wrong interpreter/venv, so it "
        "would fail or shadow the working install).",
        "Import/use it directly, or route to the owning capability; do NOT install "
        "packages ad hoc inside the loop.",
    ]
    lines.extend(provider_notes)
    observation = " ".join(lines)
    return _controlled_result(
        observation,
        command_status="blocked",
        reason="dependency_already_provided",
        summary=f"Ad-hoc install intercepted: {joined} already available",
    )


def build_shell_tools(ctx: ExecContext) -> list[Tool]:
    working_dir = ctx.working_dir or ctx.paths.project_dir

    async def bash(args: dict) -> ToolResultEnvelope:
        command = str(args.get("command", "")).strip()
        if not command:
            observation = "ERROR: empty command"
            return _controlled_result(
                observation,
                command_status="invalid",
                reason="empty_command",
                summary="Empty command",
            )
        if channel_requires_sensitive_confirm(ctx.settings, ctx.channel):
            observation = (
                "ERROR: shell commands from IM channels require local confirmation. "
                "Run the request from the CLI, or explicitly disable "
                f"require_sensitive_confirm for channel '{ctx.channel}'."
            )
            return _controlled_result(
                observation,
                command_status="blocked",
                reason="channel_confirmation_required",
                summary="Shell command requires local confirmation",
            )
        provided = _dependency_install_interception(command, ctx)
        if provided is not None:
            return provided
        mode = ctx.settings.security.bash_sandbox
        if mode != "full":
            if command_is_system_blocked(command):
                observation = (
                    "ERROR: command blocked by sandbox (system/privileged pattern that "
                    "escapes the working directory). "
                    f"Current bash_sandbox='{mode}'. Set security.bash_sandbox='full' to allow."
                )
                return _controlled_result(
                    observation,
                    command_status="blocked",
                    reason="sandbox_blocked",
                    summary="Command blocked by sandbox",
                )
            if mode == "readonly" and command_is_destructive(command):
                observation = (
                    "ERROR: command blocked by sandbox (destructive pattern) in read-only "
                    f"mode. Current bash_sandbox='{mode}'. Set security.bash_sandbox="
                    "'workspace-write' to allow destructive commands inside the working "
                    "directory (still subject to approval)."
                )
                return _controlled_result(
                    observation,
                    command_status="blocked",
                    reason="sandbox_blocked",
                    summary="Command blocked by sandbox",
                )
        timeout = float(args.get("timeout", 60) or 60)
        output_dir = durable_output_dir(ctx)
        env = compute_env(ctx)
        try:
            from omni.skills_runtime.exec_io import confined_exec_prefix

            source_cwd = ctx.working_dir or ctx.paths.invocation_cwd
            prefix = confined_exec_prefix(
                ctx,
                extra_writable=git_metadata_write_roots(command, source_cwd),
            )
        except SandboxUnavailableError as exc:
            observation = f"ERROR: OS sandbox required but unavailable: {exc}"
            return _controlled_result(
                observation,
                command_status="blocked",
                reason="sandbox_unavailable",
                summary="OS sandbox required but unavailable",
            )
        spawn = {
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "cwd": str(working_dir),
            "env": env,
            **process_group_options(),
        }
        proc = await spawn_user_shell(command, prefix=prefix, spawn=spawn)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            await stop_process_tree(proc, grace_seconds=0.1)
            observation = f"ERROR: command timed out after {timeout}s"
            return _controlled_result(
                observation,
                command_status="timed_out",
                reason="timeout",
                summary=f"Command timed out after {timeout}s",
            )
        except asyncio.CancelledError:
            await stop_process_tree(proc)
            raise
        stdout_text = (out or b"").decode("utf-8", errors="replace")
        stderr_text = (err or b"").decode("utf-8", errors="replace")
        text = f"{stdout_text}{stderr_text}"
        exit_code = int(proc.returncode or 0)
        registered = await register_output_dir(ctx, output_dir)
        observation = f"[exit={proc.returncode}]\n{text[:_OBSERVATION_OUTPUT_LIMIT]}"
        if registered:
            observation += (
                f"\n[registered {registered} artifact(s) from ${OMNI_OUTPUT_ENV}={output_dir}]"
            )
        succeeded = exit_code == 0
        return _command_result(
            observation,
            command_status="succeeded" if succeeded else "failed",
            reason="ok" if succeeded else "nonzero_exit",
            exit_code=exit_code,
            output=text,
            stderr=stderr_text,
            summary=(
                "Command completed successfully"
                if succeeded
                else command_exit_summary(exit_code, text, stderr_text)
            ),
        )

    output_dir = durable_output_dir(ctx)
    return [
        Tool(
            ToolSpec("bash", (
                "Run a shell command in the working directory, subject to sandbox policy. "
                f"Write durable CSV/JSON/PNG/SVG (and other deliverables) to {output_dir} "
                f"(${OMNI_OUTPUT_ENV}); that directory persists across bash calls, is "
                "readable by read_file, and is registered as this task's artifacts. "
                f"Scratch files may use {exec_tmp_dir(ctx)} ($TMPDIR). Host /tmp is not "
                "an artifact sink. "
                "For a recurring, non-destructive plain command family, optionally propose "
                "prefix_rule as a narrow argv prefix (for example ['cargo', 'test']); omit it "
                "for destructive commands, shell composition, expansion, or interpreters."
            ), {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "number", "description": "Seconds; default 60"},
                    "prefix_rule": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 2,
                        "maxItems": 16,
                        "description": (
                            "Optional argv prefix for a narrow, reusable session approval. "
                            "It is host-validated metadata and is never executed. Omit it "
                            "for destructive, compound, interpreter, or broad commands."
                        ),
                    },
                },
                "required": ["command"],
            }),
            bash,
        )
    ]
