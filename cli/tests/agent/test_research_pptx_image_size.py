"""research-pptx must not ship the unpatched image-size parsers."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[3] / "skills" / "research-pptx" / "scripts"


def test_image_size_override_points_at_the_local_stub() -> None:
    package = json.loads((SCRIPTS / "package.json").read_text(encoding="utf-8"))
    assert package["overrides"]["image-size"] == "file:vendor/image-size-stub"
    stub = SCRIPTS / "vendor" / "image-size-stub"
    assert (stub / "package.json").is_file()
    assert (stub / "index.js").is_file()
    assert "CVE-2025-71329" in (stub / "index.js").read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.skipif(
    not (SCRIPTS / "node_modules" / "pptxgenjs").is_dir(),
    reason="research-pptx renderer dependencies are not installed",
)
def test_renderer_writes_pptx_with_stubbed_image_size(tmp_path: Path) -> None:
    payload = tmp_path / "slide_data.json"
    output = tmp_path / "stub-check.pptx"
    payload.write_text(
        json.dumps(
            {
                "config": {"headerFont": "Arial", "bodyFont": "Arial"},
                "slides": [
                    {
                        "slide_type": "title",
                        "title": "Stub check",
                        "subtitle": "image-size unused",
                        "dark_background": True,
                    },
                    {
                        "slide_type": "content",
                        "title": "Renderer still writes a deck",
                        "bullets": ["Explicit width and height", "No image-size parsers"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        ["node", str(SCRIPTS / "generate_slides.js"), str(payload), str(output)],
        cwd=str(SCRIPTS),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert output.is_file()
    assert output.stat().st_size > 1000
