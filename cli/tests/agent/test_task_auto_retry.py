"""Task auto-retry on transient failure (P2)."""

from __future__ import annotations

import sys

import pytest

from omni.config import load_settings
from omni.runtime.notifications import InboxNotifier
from omni.runtime.subtask_runtime import SubtaskRuntime, is_transient_error
from omni.skills_runtime.context import ExecContext
from omni.skills_runtime.manifest import DeliveryMode, ExecSpec, SkillEntry, SkillKind
from omni.skills_runtime.registry import SkillRegistry
from omni.storage.db import get_database


def _cli_skill(name: str, script: str) -> SkillEntry:
    return SkillEntry(
        name=name, description="d", kind=SkillKind.CLI_EXEC,
        delivery_mode=DeliveryMode.ASYNC_TASK,
        exec_spec=ExecSpec(command=sys.executable, args=["-c", script], stdout_format="json"),
        execution={"replay_safe": True},
    )


_TRANSIENT = "import sys;sys.stdin.read();print('{\"status\":\"error\",\"error\":\"connection timeout (transient)\"}')"
_PERMANENT = "import sys;sys.stdin.read();print('{\"status\":\"error\",\"error\":\"invalid input: missing field\"}')"
# Fails transiently on the first run, then succeeds — models a flaky blip.
_HEAL = (
    "import sys,json,os;d=json.load(sys.stdin);p=d['counter'];"
    "n=(int(open(p).read()) if os.path.exists(p) else 0)+1;open(p,'w').write(str(n));"
    "print(json.dumps({'status':'ok','summary':'healed %d'%n}) if n>1 "
    "else json.dumps({'status':'error','error':'connection timeout (transient)'}))"
)


async def _runtime(**skills: str):
    s = load_settings()
    s.paths.ensure_dirs()
    db = get_database(s.paths.project_db)
    await db.init()
    reg = SkillRegistry(s)
    reg.build_index()
    for name, script in skills.items():
        reg.register(_cli_skill(name, script))
    inbox = InboxNotifier(s.paths.project_dir / "inbox.jsonl")

    def ctx_factory(session_id, channel):  # noqa: ANN001, ANN202
        return ExecContext(settings=s, paths=s.paths, session_id=session_id, channel=channel)

    return SubtaskRuntime(db, s, reg, ctx_factory, notifier=inbox), s


def test_is_transient_error_classifies():
    assert is_transient_error("connection timeout")
    assert is_transient_error("upstream 503 Service Unavailable")
    assert is_transient_error("HTTP 429 Too Many Requests")
    assert is_transient_error("read error: connection reset")
    # deterministic failures are not transient
    assert not is_transient_error("invalid input: missing required field")
    assert not is_transient_error("unknown skill 'foo'")
    assert not is_transient_error("")


@pytest.mark.asyncio
async def test_transient_failure_auto_retries_then_exhausts():
    """A perpetually-transient task retries up to the cap, then fails terminally
    (bounded — never an infinite retry storm)."""
    rt, s = await _runtime(flaky=_TRANSIENT)
    assert s.tasks.max_auto_retries == 2
    tid = await rt.enqueue("flaky", {}, "cli")
    await rt.drain()
    task = await rt.get_subtask(tid)
    assert task.status == "failed"
    assert task.recovery_attempt == s.tasks.max_auto_retries  # retried exactly the cap
    assert task.recovery_policy == "auto_retry_transient"
    assert "transient" in (task.original_error or "")


@pytest.mark.asyncio
async def test_transient_failure_self_heals_when_it_later_succeeds(tmp_path):
    """A blip that clears on retry ends up succeeded — the flake is invisible."""
    rt, _s = await _runtime(healer=_HEAL)
    counter = str(tmp_path / "count.txt")
    tid = await rt.enqueue("healer", {"counter": counter}, "cli")
    await rt.drain()
    task = await rt.get_subtask(tid)
    assert task.status == "succeeded"
    assert task.recovery_attempt == 1  # one retry was enough
    assert "healed 2" in task.result_json["summary"]


@pytest.mark.asyncio
async def test_permanent_failure_is_not_retried():
    """A deterministic failure fails fast — no wasted retries, no masking."""
    rt, _s = await _runtime(broken=_PERMANENT)
    tid = await rt.enqueue("broken", {}, "cli")
    await rt.drain()
    task = await rt.get_subtask(tid)
    assert task.status == "failed"
    assert task.recovery_attempt == 0
    assert "invalid input" in (task.error or "")


@pytest.mark.asyncio
async def test_auto_retry_can_be_disabled():
    rt, s = await _runtime(flaky=_TRANSIENT)
    s.tasks.auto_retry = False
    tid = await rt.enqueue("flaky", {}, "cli")
    await rt.drain()
    task = await rt.get_subtask(tid)
    assert task.status == "failed"
    assert task.recovery_attempt == 0  # never retried


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_status", "durable_status"),
    [
        ("failed", "failed"),
        ("blocked", "failed"),
        ("cancelled", "cancelled"),
        ("timed_out", "failed"),
    ],
)
async def test_all_explicit_domain_failures_remain_failures_in_durable_state(
    provider_status: str,
    durable_status: str,
) -> None:
    script = (
        "import sys,json;sys.stdin.read();"
        f"print(json.dumps({{'status':{provider_status!r},'error':'domain stopped'}}))"
    )
    runtime, _settings = await _runtime(domain_failure=script)

    task_id = await runtime.enqueue("domain_failure", {}, "cli")
    await runtime.drain()
    task = await runtime.get_subtask(task_id)

    assert task.status == durable_status
    assert task.result_json["status"] == provider_status
    assert "domain stopped" in (task.error or "")
