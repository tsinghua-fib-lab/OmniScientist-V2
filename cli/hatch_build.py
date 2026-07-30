"""Build hook: bundle the sibling skill collection into the wheel.

The skills live at ``<repo>/skills`` — a sibling of this Python project
(``<repo>/cli``) — so CLI code and skill content can be versioned and edited
independently. At wheel-build time we force-include that sibling under
``omni/data/skills`` so an installed copy carries the default skills with it.
Paper Review's large immutable retrieval generations are the one exception:
their small integrity manifests ship, while first use fetches the pinned
data-only repository into Omni's cache.

Editable/source installs don't run this hook; ``omni.data`` resolves the
sibling ``skills`` directory at runtime instead.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


def _is_lazy_paper_review_data(path: Path, skill_dir: Path) -> bool:
    """Package only integrity headers from Paper Review's data directory."""

    if skill_dir.name != "paper-review":
        return False
    parts = path.relative_to(skill_dir).parts
    if len(parts) < 2 or parts[:2] != ("resources", "indexes"):
        return False
    return not (len(parts) == 4 and parts[3] == "index.json")


def _force_include_skill(
    force: dict[str, str],
    *,
    skill_dir: Path,
    destination: str,
) -> None:
    """Include one skill, expanding Paper Review so its data can be skipped."""

    if skill_dir.name != "paper-review":
        force[str(skill_dir)] = destination
        return
    for path in sorted(skill_dir.rglob("*")):
        if not path.is_file() or _is_lazy_paper_review_data(path, skill_dir):
            continue
        relative = path.relative_to(skill_dir).as_posix()
        force[str(path)] = f"{destination}/{relative}"


def _indexed_skill_dirs(skills: Path) -> tuple[Path, list[Path]]:
    """Use the runtime's stdlib-only parser without importing the Omni package."""
    parser_path = Path(__file__).parent / "src" / "omni" / "skills_runtime" / "index_format.py"
    spec = importlib.util.spec_from_file_location("omni_skill_index_format", parser_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load built-in skill index parser {parser_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return skills / module.SKILL_INDEX_FILENAME, module.indexed_skill_dirs(skills)


def _validate_bundled_personas(skill_dirs: list[Path]) -> None:
    """Fail the build before packaging a damaged or incomplete persona snapshot."""
    soulagent = next((path for path in skill_dirs if path.name == "soulagent"), None)
    if soulagent is None:
        return
    validator_path = Path(__file__).parent / "src" / "omni" / "personas" / "bundle_format.py"
    spec = importlib.util.spec_from_file_location("omni_persona_bundle_format", validator_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load bundled persona validator {validator_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    resource_root = soulagent / "assets" / "builtin-scientist-kg"
    try:
        module.validate_builtin_persona_collection(resource_root)
    except module.BundledPersonaValidationError as exc:
        raise RuntimeError(f"refusing to package invalid bundled scientist personas: {exc}") from exc


class CustomBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict) -> None:  # noqa: ARG002
        force = build_data.setdefault("force_include", {})

        # Source checkouts keep skills beside ``cli``. An unpacked sdist keeps a
        # self-contained copy at ``skills`` so a wheel built from that sdist has
        # exactly the same runtime assets as a wheel built from the checkout.
        candidates = (Path(self.root).parent / "skills", Path(self.root) / "skills")
        skills = next((path.resolve() for path in candidates if path.is_dir()), None)
        if skills is not None:
            index, skill_dirs = _indexed_skill_dirs(skills)
            _validate_bundled_personas(skill_dirs)
            index_destination = (
                "skills/index.toml"
                if self.target_name == "sdist"
                else "omni/data/skills/index.toml"
            )
            force[str(index)] = index_destination
            for skill_dir in skill_dirs:
                host_modules = next(
                    (
                        path
                        for path in skill_dir.rglob("node_modules")
                        if path.is_dir()
                    ),
                    None,
                )
                if host_modules is not None:
                    raise RuntimeError(
                        "refusing to package host-specific node_modules from "
                        f"{host_modules}; use the owner-managed Skill runtime cache"
                    )
                destination = (
                    f"skills/{skill_dir.name}"
                    if self.target_name == "sdist"
                    else f"omni/data/skills/{skill_dir.name}"
                )
                _force_include_skill(
                    force,
                    skill_dir=skill_dir,
                    destination=destination,
                )

        # Bundle omni's own docs (``cli/docs``) as the read-only self-knowledge
        # corpus so ``docs_search`` / ``docs_read`` work in installed copies.
        docs = (Path(self.root) / "docs").resolve()
        if self.target_name == "wheel" and docs.is_dir():
            for md in sorted(docs.rglob("*.md")):
                force[str(md)] = f"omni/data/docs/{md.relative_to(docs)}"
