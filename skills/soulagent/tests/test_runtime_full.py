from __future__ import annotations

import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

SKILL_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SKILL_DIR.parents[1]
EXAMPLE_KG_ROOT = SKILL_DIR / "assets" / "builtin-scientist-kg"

sys.path.insert(0, str(SKILL_DIR))
try:
    import core
    import graph_pruner
    import kg_loader
    import stoma_writer
    import task_sensor
finally:
    sys.path.pop(0)


def _decoder_completion(_system_prompt: str, _user_prompt: str) -> str:
    return """## 当前人格：测试科学家
### 表达语气
由程序注入。
### 核心原则
由程序注入。
### 当前任务中的思考方式
- 先建立最简单且可证伪的基线，再隔离变量。
### 当前取舍
当前没有触发需要消解的取舍。
### 证据来源
- 测试证据：使用可控比较。
"""


class ComponentContractsTest(unittest.TestCase):
    def test_bundled_kgs_prune_every_phase_with_complete_l3_kernel(self) -> None:
        inventory = core.list_scientists(EXAMPLE_KG_ROOT)
        self.assertEqual(
            {row["scientist_id"] for row in inventory["scientists"]},
            {
                "alan-turing",
                "claude-shannon",
                "fengli-xu",
                "herbert-a-simon",
                "john-von-neumann",
                "kaiming-he",
                "norbert-wiener",
                "richard-feynman",
            },
        )
        self.assertEqual(inventory["invalid"], [])

        for scientist_id in ("fengli-xu", "kaiming-he"):
            kg = kg_loader.load_kg(EXAMPLE_KG_ROOT / scientist_id)
            expected_tone = next(
                node["tone_exemplars"]
                for node in kg["l3"]
                if node["question"] == "P04"
            )
            for phase in graph_pruner.PHASE_TO_L2:
                with self.subTest(scientist=scientist_id, phase=phase):
                    subgraph = graph_pruner.prune_graph(
                        {
                            "phase": phase,
                            "objective": "诊断模型、选择方法并设计可控消融实验",
                            "constraints": {
                                "compute_constraint": True,
                                "time_pressure": True,
                            },
                        },
                        kg,
                    )
                    self.assertGreaterEqual(len(subgraph["active_l2"]), 2)
                    self.assertLessEqual(len(subgraph["active_l2"]), 5)
                    self.assertEqual(
                        {node["question"] for node in subgraph["l3"]},
                        {"P01", "P02", "P03"},
                    )
                    self.assertEqual(
                        subgraph["philosophy_kernel"]["tone_exemplars"],
                        expected_tone,
                    )
                    self.assertLessEqual(
                        len(subgraph["l1_evidence"]),
                        len(subgraph["active_l2"]) * 5,
                    )

    def test_task_sensor_llm_semantics_and_keyword_fallback(self) -> None:
        def experiment_sensor(_system: str, _user: str) -> str:
            return json.dumps(
                {
                    "is_scientific": True,
                    "phase": "experiment_design",
                    "constraints": {
                        "compute_constraint": True,
                        "time_pressure": True,
                    },
                }
            )

        sensed = task_sensor.sense_task(
            "失败已经定位，接下来设计消融；只能单卡并且今天必须完成。",
            completion_fn=experiment_sensor,
        )
        self.assertIsNotNone(sensed)
        assert sensed is not None
        self.assertEqual(sensed["phase"], "experiment_design")
        self.assertEqual(
            sensed["constraints"],
            {"compute_constraint": True, "time_pressure": True},
        )

        non_scientific = task_sensor.sense_task(
            "把 README 标题改短",
            completion_fn=lambda _system, _user: json.dumps(
                {
                    "is_scientific": False,
                    "phase": "general",
                    "constraints": {
                        "compute_constraint": False,
                        "time_pressure": False,
                    },
                }
            ),
        )
        self.assertIsNone(non_scientific)

        fallback = task_sensor.sense_task(
            "请设计 baseline 和消融实验，GPU 不够而且赶 deadline。",
            completion_fn=lambda _system, _user: "not-json",
        )
        self.assertIsNotNone(fallback)
        assert fallback is not None
        self.assertEqual(fallback["phase"], "experiment_design")
        self.assertEqual(
            fallback["constraints"],
            {"compute_constraint": True, "time_pressure": True},
        )

        constraint_fallback = task_sensor.sense_task(
            "实验只能使用单卡 GPU，并且必须在 8 小时内完成。",
            completion_fn=lambda _system, _user: "not-json",
        )
        self.assertIsNotNone(constraint_fallback)
        assert constraint_fallback is not None
        self.assertEqual(
            constraint_fallback["constraints"],
            {"compute_constraint": True, "time_pressure": True},
        )


class StomaProtocolTest(unittest.TestCase):
    def test_every_host_writes_only_its_stoma_and_restores_original(self) -> None:
        all_names = [names[0] for names in stoma_writer.HOST_STOMA.values()]
        for host, names in stoma_writer.HOST_STOMA.items():
            target_name = names[0]
            with self.subTest(host=host), tempfile.TemporaryDirectory(
                prefix=f"造口 {host}(测试)-", dir=Path(__file__).parent
            ) as temporary:
                project = Path(temporary)
                originals = {
                    name: f"原始内容：{name}\n" for name in all_names
                }
                for name, content in originals.items():
                    (project / name).write_text(content, encoding="utf-8")

                paths = stoma_writer.write_persona(project, "临时科学家人格", host)
                self.assertEqual(set(paths), {target_name})
                self.assertEqual(
                    (project / target_name).read_text(encoding="utf-8"),
                    "临时科学家人格\n",
                )
                for name in set(all_names) - {target_name}:
                    self.assertEqual(
                        (project / name).read_text(encoding="utf-8"),
                        originals[name],
                    )

                stoma_writer.unload_persona(project, host)
                for name, content in originals.items():
                    self.assertEqual(
                        (project / name).read_text(encoding="utf-8"), content
                    )
                self.assertFalse((project / f"{target_name}.soulagent.bak").exists())

    def test_existing_writing_lock_times_out_without_touching_stoma(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="锁 (测试)-", dir=Path(__file__).parent
        ) as temporary:
            project = Path(temporary)
            role = project / "role.md"
            role.write_text("原始人格\n", encoding="utf-8")
            lock_dir = project / ".soulagent" / "lock"
            lock_dir.mkdir(parents=True)
            (lock_dir / "writing").write_text("busy", encoding="utf-8")

            with self.assertRaisesRegex(stoma_writer.StomaError, "writing 锁超时"):
                stoma_writer.write_persona(
                    project,
                    "不应写入",
                    "omniscientist",
                    lock_timeout=0.01,
                )
            self.assertEqual(role.read_text(encoding="utf-8"), "原始人格\n")

    def test_failed_rewrite_rolls_back_previous_persona_and_ready_lock(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="回滚 中文(测试)-", dir=Path(__file__).parent
        ) as temporary:
            project = Path(temporary)
            role = project / "role.md"
            role.write_text("原始人格\n", encoding="utf-8")
            stoma_writer.write_persona(project, "第一版人格", "omniscientist")
            real_atomic_write = stoma_writer._atomic_write

            def fail_new_persona(path: Path, data: bytes) -> None:
                if path == role and "第二版人格" in data.decode("utf-8"):
                    raise OSError("simulated write failure")
                real_atomic_write(path, data)

            with mock.patch.object(
                stoma_writer, "_atomic_write", side_effect=fail_new_persona
            ), self.assertRaisesRegex(stoma_writer.StomaError, "已恢复上一个版本"):
                stoma_writer.write_persona(
                    project, "第二版人格", "omniscientist"
                )

            self.assertEqual(role.read_text(encoding="utf-8"), "第一版人格\n")
            self.assertFalse((project / ".soulagent" / "lock" / "writing").exists())
            self.assertTrue((project / ".soulagent" / "lock" / "ready").is_file())
            stoma_writer.unload_persona(project, "omniscientist")
            self.assertEqual(role.read_text(encoding="utf-8"), "原始人格\n")

    def test_persona_and_state_commit_under_one_ready_lock(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="原子状态 (测试)-", dir=Path(__file__).parent
        ) as temporary:
            project = Path(temporary)
            role = project / "role.md"
            role.write_text("原始人格\n", encoding="utf-8")
            state = {
                "version": 1,
                "host": "omniscientist",
                "scientist_id": "fengli-xu",
                "scientist_name": "Fengli Xu",
            }

            paths = stoma_writer.write_persona(
                project,
                "任务人格",
                "omniscientist",
                state_payload=state,
            )

            committed = json.loads(
                (project / ".soulagent" / "state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(committed["scientist_id"], "fengli-xu")
            self.assertEqual(committed["stoma_paths"], paths)
            self.assertEqual(role.read_text(encoding="utf-8"), "任务人格\n")
            self.assertEqual(
                committed["persona_sha256"],
                hashlib.sha256(role.read_bytes()).hexdigest(),
            )
            self.assertTrue((project / ".soulagent" / "lock" / "ready").is_file())

            stoma_writer.unload_persona(
                project,
                "omniscientist",
                remove_state=True,
            )
            self.assertFalse((project / ".soulagent" / "state.json").exists())
            self.assertFalse((project / ".soulagent" / "lock" / "ready").exists())
            self.assertEqual(role.read_text(encoding="utf-8"), "原始人格\n")

    def test_failed_state_commit_restores_previous_persona_and_state(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="状态回滚 (测试)-", dir=Path(__file__).parent
        ) as temporary:
            project = Path(temporary)
            role = project / "role.md"
            role.write_text("原始人格\n", encoding="utf-8")
            first_state = {
                "host": "omniscientist",
                "scientist_id": "first",
            }
            stoma_writer.write_persona(
                project,
                "第一版人格",
                "omniscientist",
                state_payload=first_state,
            )
            state_path = project / ".soulagent" / "state.json"
            previous_state = state_path.read_bytes()
            real_atomic_write = stoma_writer._atomic_write

            def fail_second_state(path: Path, data: bytes) -> None:
                if path == state_path and b'"second"' in data:
                    raise OSError("simulated state failure")
                real_atomic_write(path, data)

            with mock.patch.object(
                stoma_writer,
                "_atomic_write",
                side_effect=fail_second_state,
            ), self.assertRaisesRegex(stoma_writer.StomaError, "已恢复上一个版本"):
                stoma_writer.write_persona(
                    project,
                    "第二版人格",
                    "omniscientist",
                    switching_scientist=True,
                    state_payload={
                        "host": "omniscientist",
                        "scientist_id": "second",
                    },
                )

            self.assertEqual(role.read_text(encoding="utf-8"), "第一版人格\n")
            self.assertEqual(state_path.read_bytes(), previous_state)
            self.assertTrue((project / ".soulagent" / "lock" / "ready").is_file())

    def test_failed_first_state_commit_leaves_no_activation_residue_and_retries(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="首次状态回滚 (测试)-", dir=Path(__file__).parent
        ) as temporary:
            project = Path(temporary)
            role = project / "role.md"
            role.write_text("原始人格\n", encoding="utf-8")
            state_path = project / ".soulagent" / "state.json"
            real_atomic_write = stoma_writer._atomic_write

            def fail_first_state(path: Path, data: bytes) -> None:
                if path == state_path:
                    raise OSError("simulated first state failure")
                real_atomic_write(path, data)

            with mock.patch.object(
                stoma_writer,
                "_atomic_write",
                side_effect=fail_first_state,
            ), self.assertRaisesRegex(stoma_writer.StomaError, "已恢复上一个版本"):
                stoma_writer.write_persona(
                    project,
                    "首次人格",
                    "omniscientist",
                    state_payload={
                        "host": "omniscientist",
                        "scientist_id": "first",
                    },
                )

            self.assertEqual(role.read_text(encoding="utf-8"), "原始人格\n")
            self.assertFalse(state_path.exists())
            self.assertFalse((project / ".soulagent" / "originals.json").exists())
            self.assertFalse((project / "role.md.soulagent.bak").exists())
            self.assertFalse((project / ".soulagent" / "lock" / "writing").exists())
            self.assertFalse((project / ".soulagent" / "lock" / "ready").exists())

            stoma_writer.write_persona(
                project,
                "首次人格",
                "omniscientist",
                state_payload={
                    "host": "omniscientist",
                    "scientist_id": "first",
                },
            )
            self.assertEqual(role.read_text(encoding="utf-8"), "首次人格\n")
            self.assertTrue(state_path.is_file())
            self.assertTrue((project / ".soulagent" / "lock" / "ready").is_file())

    def test_snapshot_failure_releases_writing_lock_and_restores_ready(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="快照异常 (测试)-", dir=Path(__file__).parent
        ) as temporary:
            project = Path(temporary)
            role = project / "role.md"
            role.write_text("原始人格\n", encoding="utf-8")
            stoma_writer.write_persona(project, "第一版人格", "omniscientist")

            with mock.patch.object(
                stoma_writer,
                "_file_snapshot",
                side_effect=OSError("simulated snapshot failure"),
            ), self.assertRaisesRegex(stoma_writer.StomaError, "准备失败"):
                stoma_writer.write_persona(
                    project,
                    "第二版人格",
                    "omniscientist",
                )

            self.assertEqual(role.read_text(encoding="utf-8"), "第一版人格\n")
            self.assertFalse((project / ".soulagent" / "lock" / "writing").exists())
            self.assertTrue((project / ".soulagent" / "lock" / "ready").is_file())

    def test_failed_rollback_never_republishes_ready(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="回滚失败 (测试)-", dir=Path(__file__).parent
        ) as temporary:
            project = Path(temporary)
            role = project / "role.md"
            role.write_text("原始人格\n", encoding="utf-8")
            state_path = project / ".soulagent" / "state.json"
            stoma_writer.write_persona(
                project,
                "第一版人格",
                "omniscientist",
                state_payload={
                    "host": "omniscientist",
                    "scientist_id": "first",
                },
            )
            real_atomic_write = stoma_writer._atomic_write
            state_commit_failed = False

            def fail_commit_and_rollback(path: Path, data: bytes) -> None:
                nonlocal state_commit_failed
                if path == state_path and b'"second"' in data:
                    state_commit_failed = True
                    raise OSError("simulated state failure")
                if (
                    state_commit_failed
                    and path == role
                    and data == "第一版人格\n".encode()
                ):
                    raise OSError("simulated rollback failure")
                real_atomic_write(path, data)

            with mock.patch.object(
                stoma_writer,
                "_atomic_write",
                side_effect=fail_commit_and_rollback,
            ), self.assertRaisesRegex(stoma_writer.StomaError, "无法完整回滚"):
                stoma_writer.write_persona(
                    project,
                    "第二版人格",
                    "omniscientist",
                    switching_scientist=True,
                    state_payload={
                        "host": "omniscientist",
                        "scientist_id": "second",
                    },
                )

            self.assertFalse((project / ".soulagent" / "lock" / "writing").exists())
            self.assertFalse((project / ".soulagent" / "lock" / "ready").exists())


class _LocalLLMHandler(http.server.BaseHTTPRequestHandler):
    requests: ClassVar[list[dict]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        self.__class__.requests.append(payload)
        system = str(payload["messages"][0]["content"])
        user = str(payload["messages"][1]["content"])
        if "scientific task sensor" in system:
            content = json.dumps(
                {
                    "is_scientific": "README" not in user,
                    "phase": "experiment_design",
                    "constraints": {
                        "compute_constraint": False,
                        "time_pressure": False,
                    },
                }
            )
        else:
            content = _decoder_completion(system, user)
        body = json.dumps(
            {"choices": [{"message": {"content": content}}]},
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class PortableCliLifecycleTest(unittest.TestCase):
    def test_full_cli_lifecycle_over_local_openai_compatible_endpoint(self) -> None:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _LocalLLMHandler)
        _LocalLLMHandler.requests = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(
                prefix="中文 项目(全功能)-", dir=Path(__file__).parent
            ) as temporary:
                project = Path(temporary)
                shutil.copytree(EXAMPLE_KG_ROOT, project / "scientist-kg")
                (project / "scientist-kg" / "invalid").mkdir()
                original_role = "原始 OmniScientist 人格。\n"
                (project / "role.md").write_text(original_role, encoding="utf-8")

                env = os.environ.copy()
                env.update(
                    {
                        "PYTHONUTF8": "1",
                        "PYTHONIOENCODING": "utf-8",
                        "SOULAGENT_API_KEY": "offline-test-key",
                        "SOULAGENT_MODEL": "offline-test-model",
                        "SOULAGENT_BASE_URL": (
                            f"http://127.0.0.1:{server.server_port}/v1"
                        ),
                    }
                )

                def run_cli(*arguments: str) -> tuple[subprocess.CompletedProcess[str], dict]:
                    completed = subprocess.run(
                        [
                            sys.executable,
                            str(SKILL_DIR / "core.py"),
                            "--project-root",
                            str(project),
                            *arguments,
                        ],
                        cwd=REPO_ROOT,
                        env=env,
                        shell=False,
                        text=True,
                        encoding="utf-8",
                        capture_output=True,
                        timeout=30,
                        check=False,
                    )
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    return completed, json.loads(completed.stdout)

                _listed_process, listed = run_cli("list")
                self.assertEqual(len(listed["scientists"]), 8)
                self.assertEqual(len(listed["invalid"]), 1)
                for scientist in listed["scientists"]:
                    scientist_path = Path(scientist["path"])
                    self.assertTrue(scientist_path.is_dir())
                    self.assertEqual(scientist_path.parent, project / "scientist-kg")

                activate_args = (
                    "activate",
                    "--scientist-id",
                    "kaiming-he",
                    "--host",
                    "omniscientist",
                    "--conversation",
                    "Design a baseline and ablation experiment for this network.",
                )
                _, activated = run_cli(*activate_args)
                self.assertEqual(activated["status"], "refreshed")
                state = json.loads(
                    (project / ".soulagent" / "state.json").read_text(encoding="utf-8")
                )
                self.assertEqual(
                    state["decoder_contract_version"], core.DECODER_CONTRACT_VERSION
                )
                kaiming_kg = kg_loader.load_kg(
                    project / "scientist-kg" / "kaiming-he"
                )
                kaiming_tone = next(
                    node["tone_exemplars"]
                    for node in kaiming_kg["l3"]
                    if node["question"] == "P04"
                )
                role_text = (project / "role.md").read_text(encoding="utf-8")
                self.assertTrue(all(value in role_text for value in kaiming_tone))

                decoder_requests = [
                    request
                    for request in _LocalLLMHandler.requests
                    if "KG解码器" in str(request["messages"][0]["content"])
                ]
                sensor_requests = [
                    request
                    for request in _LocalLLMHandler.requests
                    if "scientific task sensor"
                    in str(request["messages"][0]["content"])
                ]
                self.assertEqual(len(decoder_requests), 1)
                self.assertEqual(len(sensor_requests), 1)
                self.assertEqual(sensor_requests[0]["max_tokens"], 512)
                self.assertEqual(decoder_requests[0]["max_tokens"], 8192)
                decoder_wire_text = json.dumps(decoder_requests[0], ensure_ascii=False)
                self.assertTrue(all(value not in decoder_wire_text for value in kaiming_tone))

                _, unchanged = run_cli(*activate_args)
                self.assertEqual(unchanged["status"], "unchanged_task")

                _, forced = run_cli(*activate_args, "--force")
                self.assertEqual(forced["status"], "refreshed")

                switch_args = (
                    "activate",
                    "--scientist-id",
                    "fengli-xu",
                    "--host",
                    "omniscientist",
                    "--conversation",
                    "Design a baseline and ablation experiment for this network.",
                )
                _, switched = run_cli(*switch_args)
                self.assertEqual(switched["status"], "refreshed")
                fengli_kg = kg_loader.load_kg(
                    project / "scientist-kg" / "fengli-xu"
                )
                fengli_tone = next(
                    node["tone_exemplars"]
                    for node in fengli_kg["l3"]
                    if node["question"] == "P04"
                )
                switched_role = (project / "role.md").read_text(encoding="utf-8")
                self.assertTrue(all(value in switched_role for value in fengli_tone))

                _, non_scientific = run_cli(
                    "activate",
                    "--scientist-id",
                    "fengli-xu",
                    "--host",
                    "omniscientist",
                    "--conversation",
                    "把 README 标题改短",
                )
                self.assertEqual(non_scientific["status"], "no_scientific_task")
                self.assertEqual(
                    (project / "role.md").read_text(encoding="utf-8"), switched_role
                )

                _, status = run_cli("status")
                self.assertEqual(status["scientist_id"], "fengli-xu")

                _, unloaded = run_cli("unload")
                self.assertEqual(unloaded["status"], "unloaded")
                self.assertEqual(
                    (project / "role.md").read_text(encoding="utf-8"), original_role
                )
                self.assertFalse((project / ".soulagent" / "state.json").exists())

                _, inactive = run_cli("status")
                self.assertEqual(inactive, {"status": "inactive"})

                def run_portable(payload: dict) -> dict:
                    completed = subprocess.run(
                        [
                            sys.executable,
                            str(SKILL_DIR / "scripts" / "run.py"),
                            "--json",
                            json.dumps(payload, ensure_ascii=False),
                        ],
                        cwd=SKILL_DIR,
                        env=env,
                        shell=False,
                        text=True,
                        encoding="utf-8",
                        capture_output=True,
                        timeout=30,
                        check=True,
                    )
                    return json.loads(completed.stdout)

                portable_list = run_portable(
                    {"action": "list", "project_root": str(project)}
                )
                self.assertEqual(len(portable_list["scientists"]), 8)
                portable_activate = run_portable(
                    {
                        "action": "activate",
                        "project_root": str(project),
                        "scientist_id": "kaiming-he",
                        "host": "omniscientist",
                        "conversation": "Design a controlled ablation experiment.",
                    }
                )
                self.assertEqual(portable_activate["status"], "refreshed")
                portable_role = (project / "role.md").read_text(encoding="utf-8")
                self.assertTrue(all(value in portable_role for value in kaiming_tone))
                portable_status = run_portable(
                    {"action": "status", "project_root": str(project)}
                )
                self.assertEqual(portable_status["scientist_id"], "kaiming-he")
                portable_unload = run_portable(
                    {"action": "unload", "project_root": str(project)}
                )
                self.assertEqual(portable_unload["status"], "unloaded")
                self.assertEqual(
                    (project / "role.md").read_text(encoding="utf-8"), original_role
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class PortableRunnerContractTest(unittest.TestCase):
    def _run_runner(
        self, runner: Path, cwd: Path, *arguments: str
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
        return subprocess.run(
            [sys.executable, str(runner), *arguments],
            cwd=cwd,
            env=env,
            shell=False,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=30,
            check=True,
        )

    def test_self_test_and_structured_input_error(self) -> None:
        runner = SKILL_DIR / "scripts" / "run.py"
        completed = self._run_runner(runner, SKILL_DIR, "--self-test")
        self.assertEqual(
            json.loads(completed.stdout),
            {"status": "ok", "skill": "soulagent", "portable_runner": True},
        )

        invalid = self._run_runner(runner, SKILL_DIR, "--json", "{}")
        payload = json.loads(invalid.stdout)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["skill"], "soulagent")
        self.assertIn("action", payload["error"])

    def test_copied_runner_and_json_paths_survive_unicode_host_layout(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="外部 Host (便携测试)-", dir=Path(__file__).parent
        ) as temporary:
            root = Path(temporary)
            copied_skill = root / ".codex" / "skills" / "soulagent"
            shutil.copytree(
                SKILL_DIR,
                copied_skill,
                ignore=shutil.ignore_patterns("tests", "__pycache__", "*.pyc"),
            )
            project = root / "科研 项目(中文)"
            shutil.copytree(EXAMPLE_KG_ROOT, project / "scientist-kg")
            runner = copied_skill / "scripts" / "run.py"

            self_test = self._run_runner(runner, copied_skill, "--self-test")
            self.assertEqual(json.loads(self_test.stdout)["status"], "ok")

            listed = self._run_runner(
                runner,
                copied_skill,
                "--json",
                json.dumps(
                    {"action": "list", "project_root": str(project)},
                    ensure_ascii=False,
                ),
            )
            payload = json.loads(listed.stdout)
            self.assertEqual(
                {row["scientist_id"] for row in payload["scientists"]},
                {
                    "alan-turing",
                    "claude-shannon",
                    "fengli-xu",
                    "herbert-a-simon",
                    "john-von-neumann",
                    "kaiming-he",
                    "norbert-wiener",
                    "richard-feynman",
                },
            )
            self.assertTrue(
                all(Path(row["path"]).parent == project / "scientist-kg" for row in payload["scientists"])
            )

            status = self._run_runner(
                runner,
                copied_skill,
                "--json",
                json.dumps(
                    {"action": "status", "project_root": str(project)},
                    ensure_ascii=False,
                ),
            )
            self.assertEqual(json.loads(status.stdout), {"status": "inactive"})


if __name__ == "__main__":
    unittest.main()
