from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SKILL_DIR = Path(__file__).resolve().parents[1]
EXAMPLE_KG_ROOT = SKILL_DIR / "assets" / "builtin-scientist-kg"
sys.path.insert(0, str(SKILL_DIR))
try:
    import core
    import kg_loader
    import remote_registry
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


def _scientific_task(_system_prompt: str, _user_prompt: str) -> str:
    return json.dumps(
        {
            "is_scientific": True,
            "phase": "experiment_design",
            "constraints": {
                "compute_constraint": False,
                "time_pressure": False,
            },
        }
    )


def _remote_fixture(scientist_id: str = "kaiming-he"):
    source = EXAMPLE_KG_ROOT / scientist_id
    registry_url = "https://fixtures.example/raw/master/registry.json"
    manifest = (source / "manifest.json").read_bytes()
    identity = json.loads((source / "identity.json").read_text(encoding="utf-8"))
    entry = {
        "scientist_id": scientist_id,
        "scientist_name": identity["scientist_name"],
        "aliases": [*identity.get("aliases", []), "何恺明"],
        "path": f"scientists/{scientist_id}",
        "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
    }
    registry = json.dumps(
        {"schema_version": "1.0.0", "scientists": [entry]},
        ensure_ascii=False,
    ).encode("utf-8")
    payloads = {registry_url: registry}
    base = "https://fixtures.example/raw/master/"
    payloads[f"{base}scientists/{scientist_id}/manifest.json"] = manifest
    manifest_json = json.loads(manifest.decode("utf-8"))
    for file_entry in manifest_json["files"]:
        relative = str(file_entry["path"]).replace("\\", "/")
        payloads[f"{base}scientists/{scientist_id}/{relative}"] = (
            source / Path(relative)
        ).read_bytes()
    calls: list[str] = []

    def fetch(url: str, max_bytes: int) -> bytes:
        calls.append(url)
        payload = payloads.get(url)
        if payload is None:
            raise remote_registry.RemoteRegistryError(f"unexpected fixture URL: {url}")
        if len(payload) > max_bytes:
            raise remote_registry.RemoteRegistryError("fixture exceeded limit")
        return payload

    return registry_url, payloads, fetch, calls


class RemoteRegistrySecurityTest(unittest.TestCase):
    def test_download_validates_and_atomically_installs_in_unicode_path(self) -> None:
        registry_url, _payloads, fetch, calls = _remote_fixture()
        with tempfile.TemporaryDirectory(
            prefix="远端下载 中文(测试)-", dir=Path(__file__).parent
        ) as temporary:
            root = Path(temporary) / "扫描目录 (人格)"
            result = remote_registry.resolve_and_install_remote_scientist(
                root,
                "何恺明",
                registry_url=registry_url,
                fetch_bytes=fetch,
            )

            target = root / "kaiming-he"
            self.assertTrue(result["downloaded"])
            self.assertEqual(Path(result["kg_path"]), target)
            self.assertEqual(kg_loader.load_kg(target)["scientist_id"], "kaiming-he")
            self.assertGreater(len(calls), 2)
            self.assertFalse((root / ".install-kaiming-he.lock").exists())
            self.assertEqual(
                list(root.parent.glob(".scientist-kg-download-kaiming-he-*")), []
            )

    def test_registry_miss_does_not_create_scanner_root(self) -> None:
        registry_url, _payloads, fetch, _calls = _remote_fixture()
        with tempfile.TemporaryDirectory(
            prefix="远端缺失 (测试)-", dir=Path(__file__).parent
        ) as temporary:
            root = Path(temporary) / "scientist-kg"
            with self.assertRaises(remote_registry.RemoteScientistNotFound):
                remote_registry.resolve_and_install_remote_scientist(
                    root,
                    "Yann LeCun",
                    registry_url=registry_url,
                    fetch_bytes=fetch,
                )
            self.assertFalse(root.exists())

    def test_manifest_tampering_leaves_no_partial_install(self) -> None:
        registry_url, payloads, fetch, _calls = _remote_fixture()
        manifest_url = (
            "https://fixtures.example/raw/master/"
            "scientists/kaiming-he/manifest.json"
        )
        payloads[manifest_url] += b"\n"
        with tempfile.TemporaryDirectory(
            prefix="哈希篡改 中文(测试)-", dir=Path(__file__).parent
        ) as temporary:
            root = Path(temporary) / "scientist-kg"
            with self.assertRaisesRegex(
                remote_registry.RemoteInstallError, "manifest 哈希不匹配"
            ):
                remote_registry.resolve_and_install_remote_scientist(
                    root,
                    "kaiming-he",
                    registry_url=registry_url,
                    fetch_bytes=fetch,
                )
            self.assertFalse((root / "kaiming-he").exists())
            self.assertFalse((root / ".install-kaiming-he.lock").exists())

    def test_registry_rejects_path_traversal(self) -> None:
        registry_url, payloads, fetch, _calls = _remote_fixture()
        registry = json.loads(payloads[registry_url].decode("utf-8"))
        registry["scientists"][0]["path"] = "../kaiming-he"
        payloads[registry_url] = json.dumps(registry).encode("utf-8")
        with self.assertRaisesRegex(remote_registry.RemoteRegistryError, "不安全"):
            remote_registry.fetch_registry(registry_url, fetch_bytes=fetch)


class RemoteFallbackPipelineTest(unittest.TestCase):
    def test_local_hit_never_calls_remote(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="本地优先 中文(测试)-", dir=Path(__file__).parent
        ) as temporary:
            project = Path(temporary)
            shutil.copytree(EXAMPLE_KG_ROOT, project / "scientist-kg")
            with mock.patch.object(
                core,
                "resolve_and_install_remote_scientist",
                side_effect=AssertionError("local hit must not access remote"),
            ):
                result = core.run_pipeline(
                    project_root=project,
                    kg_root=project / "scientist-kg",
                    conversation="请设计一个可控消融实验",
                    scientist_id="何恺明",
                    host="omniscientist",
                    completion_fn=_decoder_completion,
                    task_completion_fn=_scientific_task,
                )
            self.assertEqual(result["status"], "refreshed")
            self.assertEqual(result["scientist_id"], "kaiming-he")
            self.assertNotIn("remote_install", result)

    def test_remote_hit_installs_then_loads(self) -> None:
        registry_url, _payloads, fetch, _calls = _remote_fixture()
        with tempfile.TemporaryDirectory(
            prefix="远端命中 中文(测试)-", dir=Path(__file__).parent
        ) as temporary:
            project = Path(temporary)
            kg_root = project / "全局扫描目录 (测试)"

            def install(root, request, **_kwargs):
                return remote_registry.resolve_and_install_remote_scientist(
                    root,
                    request,
                    registry_url=registry_url,
                    fetch_bytes=fetch,
                )

            with mock.patch.object(
                core, "resolve_and_install_remote_scientist", side_effect=install
            ):
                result = core.run_pipeline(
                    project_root=project,
                    kg_root=kg_root,
                    conversation="请设计一个可控消融实验",
                    scientist_id="何恺明",
                    host="omniscientist",
                    completion_fn=_decoder_completion,
                    task_completion_fn=_scientific_task,
                )
            self.assertEqual(result["status"], "refreshed")
            self.assertTrue(result["loaded"])
            self.assertTrue(result["remote_install"]["downloaded"])
            self.assertTrue((kg_root / "kaiming-he" / "manifest.json").is_file())

    def test_local_and_remote_miss_stops_before_sensor_and_offers_distiller(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="两级缺失 中文(测试)-", dir=Path(__file__).parent
        ) as temporary:
            project = Path(temporary)
            with mock.patch.object(
                core,
                "resolve_and_install_remote_scientist",
                side_effect=remote_registry.RemoteScientistNotFound(
                    "远端人格仓库中没有找到：Yann LeCun"
                ),
            ), mock.patch.object(
                core,
                "sense_task",
                side_effect=AssertionError("missing KG must stop before task sensing"),
            ):
                result = core.run_pipeline(
                    project_root=project,
                    kg_root=project / "scientist-kg",
                    conversation="用 Yann LeCun 的方式设计实验",
                    scientist_id="Yann LeCun",
                    host="omniscientist",
                )
            self.assertEqual(result["status"], "needs_input")
            self.assertFalse(result["loaded"])
            self.assertTrue(result["missing_kg"])
            self.assertTrue(result["needs_input"])
            self.assertTrue(result["offer_distillation"])
            self.assertTrue(result["host_must_not_fabricate"])
            self.assertEqual(
                result["action_required"]["skill"], "scientist-kg-distiller"
            )
            self.assertEqual(
                Path(result["action_required"]["distiller_input"]["install_root"]),
                project / "scientist-kg",
            )
            self.assertEqual(
                result["action_required"]["distiller_input"]["scientist"],
                "Yann LeCun",
            )
            self.assertIn("是否调用", result["message"])


if __name__ == "__main__":
    unittest.main()
