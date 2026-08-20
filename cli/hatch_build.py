"""Build hook: bundle sibling product assets into the wheel.

The skills live at ``<repo>/skills`` — a sibling of this Python project
(``<repo>/cli``) — so CLI code and skill content can be versioned and edited
independently. At wheel-build time we force-include that sibling under
``omni/data/skills`` so an installed copy carries the default skills with it.
Paper Review's large immutable retrieval generations are the one exception:
their small integrity manifests ship, while first use fetches the pinned
data-only repository into Omni's cache.

The loopback SPA is the same idea: Vite writes ``<repo>/web/dist``, which is
gitignored. Release CI runs ``cli/scripts/build_web_ui.sh`` first; this hook
then copies that directory to ``omni/data/web`` so ``pip install`` users never
need Node. Editable checkouts resolve ``web/dist`` at runtime instead.

Editable/source installs don't run this hook; ``omni.data`` resolves the
sibling ``skills`` directory at runtime instead.
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
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


def _stamp_web_version(
    force: dict[str, str], *, destination: str, version: str
) -> tempfile.TemporaryDirectory:
    """Tie the bundled SPA to this package version (not the Vite compile clock)."""
    temporary = tempfile.TemporaryDirectory(
        prefix="omni-web-", ignore_cleanup_errors=True
    )
    try:
        stamp = Path(temporary.name) / "version.json"
        stamp.write_text(
            json.dumps({"version": version}, indent=2) + "\n", encoding="utf-8"
        )
        force[str(stamp)] = f"{destination}/version.json"
    except BaseException:
        temporary.cleanup()
        raise
    return temporary


def _force_include_web_dist(
    force: dict[str, str], *, root: Path, target_name: str, version: str
) -> tempfile.TemporaryDirectory | None:
    """Copy a prebuilt SPA into the archive when ``web/dist/index.html`` exists.

    Checkout layout is ``<repo>/web/dist``; an unpacked sdist keeps the same
    files at ``web/dist`` so a wheel built from that sdist stays self-contained.
    Missing dist is not a hook error: ``check_dist.py`` is the release gate.
    """
    candidates = ((root.parent / "web" / "dist"), (root / "web" / "dist"))
    dist = next((path.resolve() for path in candidates if (path / "index.html").is_file()), None)
    if dist is None:
        return None
    destination = "web/dist" if target_name == "sdist" else "omni/data/web"
    for path in sorted(dist.rglob("*")):
        if path.is_file() and path.name != "version.json":
            force[str(path)] = f"{destination}/{path.relative_to(dist).as_posix()}"
    return _stamp_web_version(force, destination=destination, version=version)


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

    def _cleanup_web_stamps(self) -> None:
        temporary_directories = getattr(self, "_web_stamp_directories", ())
        self._web_stamp_directories = []
        for temporary in temporary_directories:
            temporary.cleanup()

    def clean(self, _build_variants: list[str]) -> None:
        """Release generated stamp files when Hatch cleans build hooks."""
        self._cleanup_web_stamps()

    def finalize(
        self,
        _build_variant: str,
        _build_data: dict,
        _artifact_path: str,
    ) -> None:
        """Release generated stamp files after Hatch has written the artifact."""
        self._cleanup_web_stamps()

    def initialize(self, _build_variant: str, build_data: dict) -> None:
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

        web_stamp = _force_include_web_dist(
            force,
            root=Path(self.root),
            target_name=self.target_name,
            version=str(self.metadata.version),
        )
        if web_stamp is not None:
            temporary_directories = getattr(self, "_web_stamp_directories", None)
            if temporary_directories is None:
                temporary_directories = []
                self._web_stamp_directories = temporary_directories
            temporary_directories.append(web_stamp)
