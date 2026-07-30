from __future__ import annotations

import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

if os.name == "nt":
    pty = None
else:
    import pty

pytestmark = pytest.mark.skipif(
    pty is None or shutil.which("tmux") is None,
    reason="nested PTY/tmux coverage requires a POSIX PTY and tmux",
)


def _tmux_config_text() -> str:
    """Build a tmux.conf compatible with old and new tmux versions.

    ``extended-keys`` already uses the xterm encoding consumed by this test on
    older tmux. Do not set ``extended-keys-format``: that option is absent from
    some GitHub-hosted runner builds, and version strings are not a reliable
    capability check across distro backports.
    """
    return (
        "set -g default-terminal 'screen-256color'\n"
        "set -s extended-keys on\n"
        "set -g allow-passthrough on\n"
        "set -as terminal-features 'xterm*:extkeys'\n"
    )


def _tmux_supports_extended_keys() -> bool:
    """Return whether tmux understands the extended-key options used here."""

    if shutil.which("tmux") is None:
        return False
    result = subprocess.run(
        ["tmux", "-V"],
        check=False,
        capture_output=True,
        text=True,
    )
    match = re.search(r"tmux\s+(\d+)\.(\d+)", result.stdout)
    return bool(match and tuple(map(int, match.groups())) >= (3, 3))


@pytest.mark.skipif(
    not _tmux_supports_extended_keys(),
    reason="Shift+Enter passthrough requires tmux 3.3 or newer",
)
def test_shift_enter_survives_a_real_pty_and_nested_tmux(tmp_path: Path) -> None:
    assert pty is not None
    socket_name = f"omni-test-{uuid.uuid4().hex}"
    config = tmp_path / "tmux.conf"
    config.write_text(_tmux_config_text(), encoding="utf-8")
    code = (
        "import asyncio\n"
        "from prompt_toolkit import PromptSession\n"
        "from prompt_toolkit.key_binding import KeyBindings\n"
        "from omni.cli.repl_composer import install_multiline_bindings\n"
        "from omni.cli.terminal_harness import TerminalKeyboardProtocol\n"
        "async def main():\n"
        "    bindings = KeyBindings()\n"
        "    def submit(event): event.current_buffer.validate_and_handle()\n"
        "    install_multiline_bindings(bindings, submit=submit)\n"
        "    session = PromptSession(multiline=True, key_bindings=bindings)\n"
        "    protocol = TerminalKeyboardProtocol(session.app.output, enabled=True)\n"
        "    protocol.start()\n"
        "    try: value = await session.prompt_async('READY>')\n"
        "    finally: protocol.stop()\n"
        "    print('RESULT:' + value.replace('\\n', '<NL>'), flush=True)\n"
        "asyncio.run(main())\n"
    )
    command = f"{shlex_quote(sys.executable)} -c {shlex_quote(code)}"
    # Unix-domain sockets have a small path limit (104 bytes on macOS), while
    # pytest's per-test directory can be much longer. The socket name is unique,
    # so the conventional short POSIX temp root is both isolated and portable.
    tmux_env = {**os.environ, "TMUX_TMPDIR": "/tmp", "TERM": "xterm-256color"}
    subprocess.run(
        [
            "tmux", "-L", socket_name, "-f", str(config),
            "new-session", "-d", "-x", "100", "-y", "30", command,
        ],
        check=True,
        capture_output=True,
        text=True,
        env=tmux_env,
    )

    pid, fd = pty.fork()
    if pid == 0:
        os.execvpe(
            "tmux",
            ["tmux", "-L", socket_name, "-f", str(config), "attach-session"],
            tmux_env,
        )

    output = b""
    try:
        deadline = time.time() + 12
        sent = False
        while time.time() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.1)
            if ready:
                chunk = os.read(fd, 8192)
                if not chunk:
                    break
                output += chunk
            if not sent and b"READY>" in output:
                os.write(fd, b"first\x1b[27;2;13~second\r")
                sent = True
            if b"RESULT:" in output:
                break
    finally:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        os.close(fd)
        subprocess.run(
            ["tmux", "-L", socket_name, "kill-server"],
            check=False,
            capture_output=True,
            env=tmux_env,
        )

    rendered = output.decode("utf-8", "replace")
    assert "RESULT:first<NL>second" in rendered, rendered[-2000:]


def test_ctrl_t_repeatedly_folds_one_clean_tmux_pane_and_preserves_draft(
    tmp_path: Path,
) -> None:
    """Capture the real terminal screen, not just cumulative escape bytes."""
    socket_name = f"omni-fold-{uuid.uuid4().hex}"
    code = (
        "import asyncio\n"
        "from omni.cli.repl_tui import ReplTui\n"
        "async def main():\n"
        "    tui = ReplTui(commands=())\n"
        "    await tui.start()\n"
        "    try:\n"
        "        tui.update_status(model='test/model', context_tokens=42, context_window=100)\n"
        "        tui.append_output('\\n'.join(f'body-{i}' for i in range(40)) + '\\n', raw=True)\n"
        "        await asyncio.sleep(12)\n"
        "    finally:\n"
        "        await tui.close()\n"
        "asyncio.run(main())\n"
    )
    command = f"{shlex_quote(sys.executable)} -c {shlex_quote(code)}"
    config = tmp_path / "tmux.conf"
    config.write_text("set -g remain-on-exit on\n", encoding="utf-8")
    source_root = Path(__file__).resolve().parents[2] / "src"
    env = {
        **os.environ,
        "PYTHONPATH": str(source_root),
        "TMUX_TMPDIR": "/tmp",
        "TERM": "xterm-256color",
    }
    subprocess.run(
        [
            "tmux", "-L", socket_name, "-f", str(config), "new-session", "-d",
            "-x", "100", "-y", "30", command,
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    def capture(*, history: bool = False) -> str:
        args = ["tmux", "-L", socket_name, "capture-pane", "-p"]
        if history:
            args.extend(["-S", "-"])
        result = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        if result.returncode != 0:
            pytest.fail(
                f"tmux capture-pane failed ({result.returncode}): {result.stderr.strip()}"
            )
        return result.stdout

    def wait_for(needle: str, *, history: bool = False) -> str:
        deadline = time.time() + 5
        snapshot = ""
        while time.time() < deadline:
            snapshot = capture(history=history)
            if needle in snapshot:
                return snapshot
            time.sleep(0.05)
        pytest.fail(f"{needle!r} did not appear in tmux pane:\n{snapshot}")

    try:
        wait_for("Ctrl+T collapse")
        subprocess.run(
            ["tmux", "-L", socket_name, "send-keys", "-l", "draft-stays"],
            check=True,
            capture_output=True,
            env=env,
        )
        wait_for("draft-stays")

        # Exercise both directions twice. Each capture is the terminal's current
        # pane state, so duplicate composer/meta rows are observable here.
        for collapsed in (True, False, True, False):
            subprocess.run(
                ["tmux", "-L", socket_name, "send-keys", "C-t"],
                check=True,
                capture_output=True,
                env=env,
            )
            action = "expand" if collapsed else "collapse"
            visible = wait_for(f"Ctrl+T {action}")
            history = capture(history=True)

            assert visible.count("test/model · ctx 42/100") == 1
            assert visible.count("draft-stays") == 1
            assert visible.count("auto mode") == 1
            if collapsed:
                assert "Ctrl+T to expand" in history
                assert "body-20" not in history
            else:
                assert "Ctrl+T to expand" not in history
                assert "body-20" in history
    finally:
        subprocess.run(
            ["tmux", "-L", socket_name, "kill-server"],
            check=False,
            capture_output=True,
            env=env,
        )


def test_full_screen_tui_keeps_auth_diagnostics_out_of_input_dock(tmp_path: Path) -> None:
    socket_name = f"omni-auth-{uuid.uuid4().hex}"
    log_path = tmp_path / "omni-tui.log"
    code = (
        "import asyncio, logging\n"
        "from omni.cli.render import error_card\n"
        "from omni.cli.repl_tui import ReplTui\n"
        "async def main():\n"
        f"    tui = ReplTui(commands=(), diagnostic_log_path={str(log_path)!r})\n"
        "    await tui.start()\n"
        "    try:\n"
        "        tui.append_output('REQUEST: RAG research workflow\\n')\n"
        "        tui.set_busy(True)\n"
        "        logging.getLogger('omni.core.react_agent').warning(\n"
        "            '[react] LLM call failed iter=1 category=authentication status=401'\n"
        "        )\n"
        "        tui.set_busy(False)\n"
        "        error_card(\n"
        "            'Model authentication failed',\n"
        "            'The configured provider rejected the active credential.',\n"
        "            actions=('Check it with `/config test`.',),\n"
        "        )\n"
        "        value = await tui.read_line_async(mode='auto', fallback=lambda: '')\n"
        "    finally:\n"
        "        await tui.close()\n"
        "    print('RESULT:' + value, flush=True)\n"
        "asyncio.run(main())\n"
    )
    command = f"{shlex_quote(sys.executable)} -c {shlex_quote(code)}"
    source_root = Path(__file__).resolve().parents[2] / "src"
    env = {
        **os.environ,
        "TMUX_TMPDIR": "/tmp",
        "TERM": "xterm-256color",
        "PYTHONPATH": str(source_root),
    }
    subprocess.run(
        [
            "tmux", "-L", socket_name, "new-session", "-d",
            "-x", "100", "-y", "30", command,
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    subprocess.run(
        ["tmux", "-L", socket_name, "set-option", "-g", "remain-on-exit", "on"],
        check=True,
        capture_output=True,
        env=env,
    )

    captured = ""
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            result = subprocess.run(
                ["tmux", "-L", socket_name, "capture-pane", "-p", "-S", "-"],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            captured = result.stdout
            if "Model authentication failed" in captured and "Enter send" in captured:
                break
            time.sleep(0.1)

        assert "Model authentication failed" in captured
        assert "/config test" in captured
        assert "status=401" not in captured
        assert "REQUEST: RAG research workflow" in captured
        assert "Enter send" in captured

        subprocess.run(
            ["tmux", "-L", socket_name, "send-keys", "next question", "Enter"],
            check=True,
            capture_output=True,
            env=env,
        )
        deadline = time.time() + 5
        while time.time() < deadline:
            result = subprocess.run(
                ["tmux", "-L", socket_name, "capture-pane", "-p", "-S", "-"],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            captured = result.stdout
            if "RESULT:next question" in captured:
                break
            time.sleep(0.1)
        assert "RESULT:next question" in captured
    finally:
        subprocess.run(
            ["tmux", "-L", socket_name, "kill-server"],
            check=False,
            capture_output=True,
            env=env,
        )

    diagnostics = log_path.read_text(encoding="utf-8")
    assert "status=401" in diagnostics


def test_real_cli_auth_failure_is_actionable_and_keeps_composer_usable(tmp_path: Path) -> None:
    calls: list[str] = []

    class UnauthorizedHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            calls.append(self.path)
            length = int(self.headers.get("content-length", "0") or 0)
            self.rfile.read(length)
            body = b'{"error":{"message":"invalid API key; status=401"}}'
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), UnauthorizedHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    omni_home = tmp_path / "omni-home"
    user_home = tmp_path / "user-home"
    work_dir = tmp_path / "workspace"
    omni_home.mkdir()
    user_home.mkdir()
    work_dir.mkdir()
    port = server.server_address[1]
    # Pre-trust the launch directory: the workspace-trust gate would otherwise
    # block the first interactive run on a "Do you trust this folder?" prompt,
    # and the composer ("Enter send") would never appear. Power-user/automation
    # opt-in via ``[trust] allow`` (a user-config-only key).
    (omni_home / "config.toml").write_text(
        "[model]\n"
        "provider = \"openai\"\n"
        f"base_url = \"http://127.0.0.1:{port}/v1\"\n"
        "model = \"test-model\"\n"
        "\n[display]\n"
        "ui_mode = \"tui\"\n"
        "verbosity = \"normal\"\n"
        "\n[memory]\n"
        "embeddings_enabled = false\n"
        "\n[service]\n"
        # This test exercises TUI auth UX in a throwaway OMNI_HOME. Never let the
        # bare-omni ensure hook install a KeepAlive LaunchAgent into the host.
        "ensure_on_launch = false\n"
        "\n[trust]\n"
        f"allow = [{json.dumps(str(work_dir))}]\n",
        encoding="utf-8",
    )
    (omni_home / "secrets.toml").write_text(
        "[model]\napi_key = \"invalid\"\n",
        encoding="utf-8",
    )

    socket_name = f"omni-real-auth-{uuid.uuid4().hex}"
    source_root = Path(__file__).resolve().parents[2] / "src"
    env = {
        **os.environ,
        "HOME": str(user_home),
        "OMNI_HOME": str(omni_home),
        "PYTHONPATH": str(source_root),
        "TMUX_TMPDIR": "/tmp",
        "TERM": "xterm-256color",
    }
    command = f"{shlex_quote(sys.executable)} -m omni.cli.main --ui tui"
    subprocess.run(
        [
            "tmux", "-L", socket_name, "new-session", "-d",
            "-c", str(work_dir),
            "-x", "120", "-y", "36", command,
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    subprocess.run(
        ["tmux", "-L", socket_name, "set-option", "-g", "remain-on-exit", "on"],
        check=True,
        capture_output=True,
        env=env,
    )

    prompt = (
        "为 RAG 系统综述准备材料：获取 Attention Is All You Need 摘要，并生成包含 "
        "query、retriever、reranker、LLM 的科研架构图。并输出一篇论文"
    )
    captured = ""
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            result = subprocess.run(
                ["tmux", "-L", socket_name, "capture-pane", "-p", "-S", "-"],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            captured = result.stdout
            if "Enter send" in captured:
                break
            time.sleep(0.1)
        assert "Enter send" in captured

        subprocess.run(
            ["tmux", "-L", socket_name, "send-keys", "-l", prompt],
            check=True,
            capture_output=True,
            env=env,
        )
        subprocess.run(
            ["tmux", "-L", socket_name, "send-keys", "Enter"],
            check=True,
            capture_output=True,
            env=env,
        )

        deadline = time.time() + 30
        while time.time() < deadline:
            result = subprocess.run(
                ["tmux", "-L", socket_name, "capture-pane", "-p", "-S", "-"],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            captured = result.stdout
            if "Model authentication failed" in captured and "Enter send" in captured:
                break
            time.sleep(0.1)

        assert captured.lower().count("model authentication failed") == 1
        assert "/config test" in captured
        assert "/config model" in captured
        assert "status=401" not in captured
        assert "traceback" not in captured.lower()
        assert "Enter send" in captured
        assert calls == ["/v1/chat/completions"]

        subprocess.run(
            ["tmux", "-L", socket_name, "send-keys", "/exit", "Enter"],
            check=False,
            capture_output=True,
            env=env,
        )
    finally:
        subprocess.run(
            ["tmux", "-L", socket_name, "kill-server"],
            check=False,
            capture_output=True,
            env=env,
        )
        # Belt-and-suspenders: if an older build still bootstrapped launchd for
        # this throwaway home, boot it out before tmp_path is deleted.
        _scrub_ephemeral_home_service(omni_home, env=env, python=sys.executable)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    diagnostics = (omni_home / "logs" / "omni-tui.log").read_text(encoding="utf-8")
    assert "status=401" in diagnostics
    assert "invalid" not in diagnostics.lower()


def _scrub_ephemeral_home_service(
    omni_home: Path, *, env: dict[str, str], python: str
) -> None:
    """Best-effort disable + orphan prune for a test OMNI_HOME."""
    scrub_env = {**env, "OMNI_HOME": str(omni_home)}
    subprocess.run(
        [python, "-m", "omni.cli.main", "serve", "stop"],
        check=False,
        capture_output=True,
        text=True,
        env=scrub_env,
    )
    subprocess.run(
        [python, "-m", "omni.cli.main", "serve", "prune", "--orphans", "--yes"],
        check=False,
        capture_output=True,
        text=True,
        env=scrub_env,
    )


def shlex_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)
