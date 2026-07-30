"""Anonymous Arena preference memory for paper-review revision guidance.

The corpus keeps one SPECTER2-style ``title [SEP] abstract`` vector per paper.
Each retrieved packet contains complete preferred/less-preferred review pairs, but
never exposes paper titles, query ids, battle ids, or agent identities to prompts.
The memory teaches how humans prefer feedback to be written; it is not evidence
about the manuscript currently under review.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib
import json
import math
import os
import re
import shlex
import shutil
import tempfile
import time
import uuid
import zlib
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path, PurePosixPath
from typing import Any, Self

INDEX_SCHEMA_VERSION = "paper_review_preference_memory_faiss_v1"
INDEX_OWNER = "omniscientist.paper-review.preference-memory"
EMBEDDING_TEXT_POLICY = "specter2_title_sep_abstract_v1"
PREFERENCE_TEXT_POLICY = "arena_complete_anonymous_pair_v1"
DEFAULT_TOP_K = 3
MAX_TOP_K = 5
MAX_PREFERENCE_PACKET_BYTES = 64 * 1024 * 1024

_SKILL_DIR = Path(__file__).resolve().parent
_INDEX_HEADER = "index.json"
_GENERATIONS_DIR = "generations"
_VECTOR_FILE = "vectors.faiss"
_PAPERS_FILE = "papers.jsonl"
_PREFERENCES_FILE = "preferences.pack"
_INDEX_FORMAT = "faiss-idmap2-flat-ip"
_GENERATION_RE = re.compile(r"gen-[0-9a-f]{32}\Z")
_SPACE_RE = re.compile(r"\s+")
_HEADING_RE = re.compile(r"^\s*#{1,6}\s+(.+?)\s*$")
_ABSTRACT_HEADING_RE = re.compile(
    r"^\s*#{1,6}\s+(?:\d+(?:\.\d+)*[.)]?\s+)?"
    r"abstract(?:\s*[:：]\s*(.*?))?\s*$",
    re.IGNORECASE,
)
_ABSTRACT_LABEL_RE = re.compile(r"^\s*abstract\s*[:：]\s*(.*?)\s*$", re.IGNORECASE)
_ABSTRACT_AT_END_RE = re.compile(r"\babstract\s*$", re.IGNORECASE)
_INTRODUCTION_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:\d+(?:\.\d+)*[.)]?\s*)?introduction\s*$",
    re.IGNORECASE,
)

Embedder = Callable[[list[str]], Awaitable[list[list[float]]]]


def _clean_text(value: Any) -> str:
    text = str(value or "")
    return "".join(
        character
        for character in text
        if character in "\n\r\t" or ord(character) >= 32
    ).strip()


def _flat_text(value: Any) -> str:
    return _SPACE_RE.sub(" ", _clean_text(value)).strip()


def _normalized_title(value: Any) -> str:
    return _flat_text(value).casefold()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _within_root(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def paper_embedding_text(title: Any, abstract: Any) -> str:
    """Return the representation shared by Arena indexing and query retrieval."""

    return f"{_flat_text(title)} [SEP] {_flat_text(abstract)}".strip()


def preference_index_setup_command(
    dataset_path: str | Path | None = None,
    index_path: str | Path | None = None,
    *,
    rebuild: bool = False,
) -> str:
    """Return a portable command for building this memory."""

    script = shlex.quote(str(_SKILL_DIR / "scripts" / "build_preference_index.py"))
    dataset = shlex.quote(str(dataset_path or "<REVIEW_ARENA_CLEAN_DIRECTORY>"))
    index = shlex.quote(str(index_path or "<PREFERENCE_INDEX_DIRECTORY>"))
    command = f"python3 {script} --dataset {dataset} --index {index}"
    return f"{command} --rebuild" if rebuild else command


def _read_json(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"expected a regular non-symlink JSON file: {path}")
    try:
        return json.loads(path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON file: {path}") from exc


def _iter_jsonl(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"expected a regular non-symlink JSONL file: {path}")
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL record at {path}:{line_number}") from exc
            if not isinstance(row, dict):
                raise TypeError(f"JSONL record at {path}:{line_number} is not an object")
            yield line_number, row


def _response_hash(text: str) -> str:
    return _sha256_bytes(text.encode("utf-8"))


def _add_response(
    table: dict[Any, dict[str, str]],
    key: Any,
    value: Any,
) -> bool:
    """Store one complete response and report whether an exact duplicate folded."""

    text = _clean_text(value)
    if not text:
        return False
    digest = _response_hash(text)
    bucket = table.setdefault(key, {})
    duplicate = digest in bucket
    bucket.setdefault(digest, text)
    return duplicate


def _human_rows(value: Any) -> Iterable[tuple[Any, Any]]:
    if isinstance(value, list):
        for row in value:
            if isinstance(row, dict):
                yield row.get("query_id"), row.get("human_response")
        return
    if not isinstance(value, dict):
        raise TypeError("queries_human_responses.json must contain a list or object")
    for query_id, raw in value.items():
        candidates = raw if isinstance(raw, list) else [raw]
        for candidate in candidates:
            if isinstance(candidate, dict):
                yield candidate.get("query_id") or query_id, candidate.get(
                    "human_response"
                )
            else:
                yield query_id, candidate


def _normalized_markdown_relative_path(file_name: Any) -> PurePosixPath:
    value = _clean_text(file_name).replace("\\", "/")
    lowered = value.casefold()
    for prefix in ("pdf_md/", "pdf/"):
        if lowered.startswith(prefix):
            value = value[len(prefix) :]
            break
    relative = PurePosixPath(value)
    if (
        not value
        or relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError("Arena paper file name is empty, absolute, or escapes its root")
    suffix = relative.suffix.casefold()
    if suffix in {".pdf", ".md"}:
        relative = relative.with_suffix(".md")
    else:
        raise ValueError("Arena paper file name must end in .pdf or .md")
    return relative


def _resolve_markdown_path(
    file_name: Any,
    *,
    markdown_root: Path,
) -> tuple[Path, str]:
    relative = _normalized_markdown_relative_path(file_name)
    candidate = markdown_root.joinpath(*relative.parts)
    resolved = candidate.resolve()
    if not _within_root(resolved, markdown_root):
        raise ValueError("Arena paper Markdown path escapes paper_markdown")
    if candidate.is_symlink() or not resolved.is_file():
        raise FileNotFoundError(f"Arena paper Markdown is missing or unsafe: {relative}")
    return resolved, relative.as_posix()


def _markdown_title_abstract(path: Path) -> tuple[str, str]:
    """Extract title/abstract across common MinerU-style Markdown layouts."""

    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    title = ""
    for line in lines:
        match = _HEADING_RE.match(line)
        if match and not _ABSTRACT_HEADING_RE.match(line):
            title = _flat_text(match.group(1))
            break
    title = title or _flat_text(path.stem)

    start = -1
    inline = ""
    for index, line in enumerate(lines[:300]):
        heading = _ABSTRACT_HEADING_RE.match(line)
        label = _ABSTRACT_LABEL_RE.match(line)
        if heading or label:
            match = heading or label
            assert match is not None
            start = index + 1
            inline = _clean_text(match.group(1) or "")
            break
        stripped = line.strip()
        if (
            stripped
            and not _HEADING_RE.match(line)
            and _ABSTRACT_AT_END_RE.search(stripped)
        ):
            start = index + 1
            break
    if start < 0:
        return title, ""

    body: list[str] = [inline] if inline else []
    for line in lines[start:]:
        if _HEADING_RE.match(line) or _INTRODUCTION_RE.match(line):
            break
        body.append(line)
    abstract = _flat_text("\n".join(body))
    return title, abstract


def _side_response(
    battle: dict[str, Any],
    side: str,
    *,
    nonhuman: dict[tuple[str, str], dict[str, str]],
    human: dict[str, dict[str, str]],
) -> tuple[str, str] | None:
    query_id = _flat_text(battle.get("query_id"))
    key = _flat_text(battle.get(f"agent_{side}_key"))
    name = _flat_text(battle.get(f"agent_{side}_name"))
    is_human = key.casefold() == "reviewer_human" or name.casefold() == "reviewer human"
    candidates = human.get(query_id, {}) if is_human else nonhuman.get((query_id, key), {})
    if len(candidates) != 1:
        return None
    return next(iter(candidates.items()))


def parse_arena_dataset(
    dataset_path: str | Path,
    *,
    limit: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Parse and aggregate Review Arena battles into one record per paper.

    Duplicate copies of the exact same complete response are folded. A battle is
    skipped whenever either side has more than one distinct candidate response,
    because this dataset has no instance id that could resolve that ambiguity.
    """

    raw_root = Path(dataset_path).expanduser()
    if raw_root.is_symlink():
        raise ValueError("Arena dataset directory must not be a symlink")
    root = raw_root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(root)
    markdown_root = (root / "paper_markdown").resolve()
    if not markdown_root.is_dir() or not _within_root(markdown_root, root):
        raise ValueError("Arena dataset is missing a safe paper_markdown directory")
    answers_path = root / "paper_review_answers.jsonl"
    human_path = root / "queries_human_responses.json"
    battles_path = root / "reviewer_results.json"

    nonhuman: dict[tuple[str, str], dict[str, str]] = {}
    human: dict[str, dict[str, str]] = {}
    file_names: dict[str, set[str]] = defaultdict(set)
    duplicate_answers = 0
    for _line_number, row in _iter_jsonl(answers_path):
        query_id = _flat_text(row.get("query_id"))
        agent_key = _flat_text(row.get("agent_key"))
        file_name = _clean_text(row.get("file_name"))
        if query_id and file_name:
            file_names[query_id].add(file_name)
        if not query_id or not agent_key or agent_key.casefold() == "reviewer_human":
            continue
        duplicate_answers += int(
            _add_response(nonhuman, (query_id, agent_key), row.get("answer"))
        )

    for query_id_raw, response in _human_rows(_read_json(human_path)):
        query_id = _flat_text(query_id_raw)
        if query_id:
            duplicate_answers += int(_add_response(human, query_id, response))

    raw_battles = _read_json(battles_path)
    if not isinstance(raw_battles, list):
        raise TypeError("reviewer_results.json must contain a list")
    skipped: Counter[str] = Counter()
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    battles_seen = 0
    battles_resolved = 0
    query_paths: dict[str, tuple[Path, str]] = {}

    for battle in raw_battles:
        if limit is not None and battles_seen >= limit:
            break
        battles_seen += 1
        if not isinstance(battle, dict):
            skipped["malformed_battle"] += 1
            continue
        if battle.get("is_valid") is False or battle.get("is_archived") is True:
            skipped["inactive_battle"] += 1
            continue
        query_id = _flat_text(battle.get("query_id"))
        result = _flat_text(battle.get("result"))
        if not query_id or result not in {"A", "B", "Tie", "BothBad"}:
            skipped["malformed_battle"] += 1
            continue

        side_a = _side_response(
            battle, "a", nonhuman=nonhuman, human=human
        )
        side_b = _side_response(
            battle, "b", nonhuman=nonhuman, human=human
        )
        if side_a is None or side_b is None:
            ambiguous = False
            for side in ("a", "b"):
                key = _flat_text(battle.get(f"agent_{side}_key"))
                name = _flat_text(battle.get(f"agent_{side}_name"))
                candidates = (
                    human.get(query_id, {})
                    if key.casefold() == "reviewer_human"
                    or name.casefold() == "reviewer human"
                    else nonhuman.get((query_id, key), {})
                )
                ambiguous = ambiguous or len(candidates) > 1
            skipped[
                "ambiguous_response" if ambiguous else "missing_response"
            ] += 1
            continue
        hash_a, text_a = side_a
        hash_b, text_b = side_b
        if hash_a == hash_b:
            skipped["identical_responses"] += 1
            continue

        if query_id not in query_paths:
            resolved_candidates: dict[str, tuple[Path, str]] = {}
            invalid_path = False
            for file_name in file_names.get(query_id, set()):
                try:
                    resolved, relative = _resolve_markdown_path(
                        file_name, markdown_root=markdown_root
                    )
                except (FileNotFoundError, ValueError):
                    invalid_path = True
                    continue
                resolved_candidates[relative] = (resolved, relative)
            if invalid_path or len(resolved_candidates) != 1:
                skipped[
                    "ambiguous_paper_path"
                    if len(resolved_candidates) > 1
                    else "invalid_paper_path"
                ] += 1
                continue
            query_paths[query_id] = next(iter(resolved_candidates.values()))

        low, high = sorted((hash_a, hash_b))
        group_key = (query_id, low, high)
        group = groups.setdefault(
            group_key,
            {
                "responses": {hash_a: text_a, hash_b: text_b},
                "wins": Counter(),
                "tie_votes": 0,
                "both_bad_votes": 0,
                "battle_count": 0,
            },
        )
        group["battle_count"] += 1
        if result == "A":
            group["wins"][hash_a] += 1
        elif result == "B":
            group["wins"][hash_b] += 1
        elif result == "Tie":
            group["tie_votes"] += 1
        else:
            group["both_bad_votes"] += 1
        battles_resolved += 1

    paper_pairs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    pair_count = 0
    for (query_id, low, high), group in groups.items():
        low_wins = int(group["wins"].get(low, 0))
        high_wins = int(group["wins"].get(high, 0))
        if low_wins == high_wins:
            skipped["no_strict_winner_pair"] += int(group["battle_count"])
            continue
        preferred = low if low_wins > high_wins else high
        less_preferred = high if preferred == low else low
        preferred_votes = max(low_wins, high_wins)
        if int(group["both_bad_votes"]) >= preferred_votes:
            skipped["both_bad_not_outvoted"] += int(group["battle_count"])
            continue
        _path, relative = query_paths[query_id]
        paper_pairs[relative].append(
            {
                "preferred_review": group["responses"][preferred],
                "less_preferred_review": group["responses"][less_preferred],
                "preferred_votes": preferred_votes,
                "less_preferred_votes": min(low_wins, high_wins),
                "tie_votes": int(group["tie_votes"]),
                "both_bad_votes": int(group["both_bad_votes"]),
            }
        )
        pair_count += 1

    records: list[dict[str, Any]] = []
    title_only = 0
    for relative in sorted(paper_pairs):
        path = markdown_root.joinpath(*PurePosixPath(relative).parts).resolve()
        if not _within_root(path, markdown_root) or not path.is_file():
            raise ValueError("resolved Arena Markdown changed during parsing")
        title, abstract = _markdown_title_abstract(path)
        if not abstract:
            title_only += 1
        retrieval_text = paper_embedding_text(title, abstract)
        raw_packet = json.dumps(
            paper_pairs[relative],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(raw_packet) > MAX_PREFERENCE_PACKET_BYTES:
            raise ValueError(
                f"complete Arena preference packet for {relative} exceeds the safe limit"
            )
        records.append(
            {
                "paper_id": f"arena-{_sha256_bytes(relative.encode())[:24]}",
                "title": title,
                "normalized_title": _normalized_title(title),
                "abstract": abstract,
                "retrieval_text": retrieval_text,
                "retrieval_hash": _sha256_bytes(retrieval_text.encode("utf-8")),
                "paper_relative_path": relative,
                "paper_sha256": _sha256_file(path),
                "pair_count": len(paper_pairs[relative]),
                "preferences_blob": zlib.compress(raw_packet, level=6),
                "preferences_raw_bytes": len(raw_packet),
                "preferences_raw_sha256": _sha256_bytes(raw_packet),
            }
        )

    skipped_battles = sum(skipped.values())
    report = {
        "battles_seen": battles_seen,
        "battles_with_responses": battles_resolved,
        "battles_included": battles_seen - skipped_battles,
        "battles_skipped": skipped_battles,
        "skipped_by_reason": dict(sorted(skipped.items())),
        "duplicate_response_rows_folded": duplicate_answers,
        "paper_count": len(records),
        "preference_pair_count": pair_count,
        "title_only_paper_count": title_only,
    }
    return records, report


def _normalize_vector(vector: list[float]) -> list[float]:
    values = [float(item) for item in vector]
    if not values or any(not math.isfinite(item) for item in values):
        raise ValueError("embedding provider returned an empty or non-finite vector")
    scale = max(abs(item) for item in values)
    if scale <= 0.0:
        raise ValueError("embedding provider returned a zero vector")
    scaled = [item / scale for item in values]
    norm = math.sqrt(sum(item * item for item in scaled))
    normalized = [item / norm for item in scaled]
    if any(not math.isfinite(item) for item in normalized):
        raise ValueError("embedding provider returned an invalid vector")
    return normalized


def _faiss_modules() -> tuple[Any, Any]:
    try:
        return importlib.import_module("faiss"), importlib.import_module("numpy")
    except ImportError as exc:
        raise RuntimeError(
            "FAISS preference-memory support is unavailable. Install OmniScientist "
            "with the vec extra (pip install -e './cli[vec]')."
        ) from exc


def _safe_embedding_error(exc: Exception, *, detail: bool = False) -> str:
    code = str(getattr(exc, "code", "") or "")
    status = getattr(exc, "http_status", None)
    if code == "embedding_http_error" and isinstance(status, int):
        return f"embedding endpoint returned HTTP {status}"
    categories = {
        "embedding_timeout": "embedding request timed out",
        "embedding_transport_error": "embedding transport failed",
        "embedding_invalid_response": "embedding endpoint returned an invalid response",
    }
    if code in categories:
        return categories[code]
    if isinstance(exc, NotImplementedError):
        return "embedding runtime is unavailable"
    if detail and isinstance(exc, ValueError):
        return _flat_text(exc)
    return f"embedding request failed ({type(exc).__name__})"


def _read_json_object(path: Path) -> dict[str, Any]:
    value = _read_json(path)
    if not isinstance(value, dict):
        raise TypeError(f"JSON metadata is not an object: {path}")
    return value


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    encoded = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


class _BuildLock:
    def __init__(self, destination: Path) -> None:
        self._path = Path(f"{destination}.build.lock")
        self._handle: Any | None = None

    def __enter__(self) -> Self:
        if self._path.is_symlink():
            raise ValueError(f"refusing symlink build lock: {self._path}")
        self._handle = self._path.open("a+b")
        try:
            try:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except ImportError:  # pragma: no cover - Windows fallback
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
        except (BlockingIOError, OSError):
            self._handle.close()
            self._handle = None
            raise RuntimeError("another process is building this preference index") from None
        return self

    def __exit__(self, *_args: object) -> None:
        if self._handle is None:
            return
        try:
            try:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            except ImportError:  # pragma: no cover - Windows fallback
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self._handle.close()
            self._handle = None


def _read_snapshot(path: Path, *, validate_artifacts: bool = True) -> dict[str, Any]:
    if path.is_symlink() or not path.is_dir():
        raise ValueError("preference-memory index is not a regular directory")
    header = _read_json_object(path / _INDEX_HEADER)
    if header.get("index_owner") != INDEX_OWNER:
        raise ValueError("directory is not an Omni preference-memory index")
    if (
        header.get("index_format") != _INDEX_FORMAT
        or header.get("similarity") != "cosine"
        or header.get("vectors_normalized") is not True
    ):
        raise ValueError("preference-memory FAISS metadata is invalid")
    generation_name = str(header.get("active_generation") or "")
    if not _GENERATION_RE.fullmatch(generation_name):
        raise ValueError("preference-memory generation name is invalid")
    generations = path / _GENERATIONS_DIR
    generation = generations / generation_name
    if (
        generations.is_symlink()
        or not generations.is_dir()
        or generation.is_symlink()
        or not generation.is_dir()
        or not _within_root(generation.resolve(), path.resolve())
    ):
        raise ValueError("preference-memory active generation is missing or unsafe")
    declarations = header.get("artifacts")
    if not isinstance(declarations, dict):
        raise TypeError("preference-memory artifact metadata is missing")
    artifact_paths: dict[str, Path] = {}
    for filename in (_VECTOR_FILE, _PAPERS_FILE, _PREFERENCES_FILE):
        declaration = declarations.get(filename)
        artifact = generation / filename
        if (
            not isinstance(declaration, dict)
            or artifact.is_symlink()
            or not artifact.is_file()
            or not _within_root(artifact.resolve(), generation.resolve())
        ):
            raise ValueError(f"preference-memory artifact is missing or unsafe: {filename}")
        if artifact.stat().st_size != int(declaration.get("bytes", -1)):
            raise ValueError(f"preference-memory artifact size mismatch: {filename}")
        if validate_artifacts and _sha256_file(artifact) != str(
            declaration.get("sha256") or ""
        ):
            raise ValueError(f"preference-memory artifact hash mismatch: {filename}")
        artifact_paths[filename] = artifact
    return {
        **header,
        "_generation_path": generation,
        "_artifact_paths": artifact_paths,
    }


def _load_records(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    paths = snapshot["_artifact_paths"]
    pack_size = paths[_PREFERENCES_FILE].stat().st_size
    records: list[dict[str, Any]] = []
    expected_offset = 0
    seen_ids: set[str] = set()
    with paths[_PAPERS_FILE].open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                raise ValueError(f"blank preference paper-map line {line_number}")
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid preference paper-map line {line_number}") from exc
            if not isinstance(record, dict) or int(record.get("faiss_id", -1)) != len(
                records
            ):
                raise ValueError("preference-memory FAISS ids are not contiguous")
            paper_id = _flat_text(record.get("paper_id"))
            if not paper_id or paper_id in seen_ids:
                raise ValueError("preference-memory paper ids are missing or duplicated")
            seen_ids.add(paper_id)
            offset = int(record.get("preferences_offset", -1))
            length = int(record.get("preferences_length", -1))
            if offset != expected_offset or length <= 0 or offset + length > pack_size:
                raise ValueError("preference-memory packet offsets are invalid")
            if int(record.get("pair_count", 0)) <= 0:
                raise ValueError("preference-memory paper has no preference pairs")
            expected_offset += length
            records.append(record)
    if len(records) != int(snapshot.get("corpus_paper_count") or 0):
        raise ValueError("preference-memory paper count does not match metadata")
    if expected_offset != pack_size:
        raise ValueError("preference-memory map does not cover preferences.pack")
    return records


def _load_faiss_index(snapshot: dict[str, Any], *, expected_records: int) -> Any:
    faiss, _numpy = _faiss_modules()
    try:
        index = faiss.read_index(str(snapshot["_artifact_paths"][_VECTOR_FILE]))
    except Exception as exc:
        raise ValueError("preference-memory FAISS index could not be read") from exc
    if type(index).__name__ != "IndexIDMap2":
        raise ValueError("preference-memory index is not an IndexIDMap2")
    inner = faiss.downcast_index(index.index)
    if type(inner).__name__ != "IndexFlatIP":
        raise ValueError("preference-memory index is not exact IndexFlatIP")
    if (
        int(index.d) != int(snapshot.get("embedding_dimension") or 0)
        or int(index.ntotal) != expected_records
    ):
        raise ValueError("preference-memory FAISS dimensions or count are invalid")
    stored_ids = [int(item) for item in faiss.vector_to_array(index.id_map).tolist()]
    if stored_ids != list(range(expected_records)):
        raise ValueError("preference-memory FAISS id map is not canonical")
    return index


def _is_owned_index(path: Path) -> bool:
    try:
        return _read_json_object(path / _INDEX_HEADER).get("index_owner") == INDEX_OWNER
    except (OSError, TypeError, ValueError):
        return False


def _validate_destination(destination: Path) -> None:
    if destination.is_symlink():
        raise ValueError(f"refusing symlink index path: {destination}")
    if not destination.exists():
        return
    if not destination.is_dir() or not _is_owned_index(destination):
        raise ValueError("refusing to modify a path not owned by preference memory")
    try:
        _read_snapshot(destination, validate_artifacts=True)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ValueError("refusing to modify a corrupted preference-memory index") from exc


async def build_preference_index(
    dataset_path: str | Path,
    index_path: str | Path,
    *,
    embedder: Embedder,
    embedding_model: str,
    embedding_space_id: str = "",
    batch_size: int = 32,
    rebuild: bool = False,
    limit: int | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Publish a complete immutable FAISS generation over Arena preferences."""

    dataset = Path(dataset_path).expanduser().resolve()
    raw_destination = Path(index_path).expanduser()
    if raw_destination.is_symlink():
        raise ValueError(f"refusing symlink index path: {raw_destination}")
    destination = raw_destination.resolve()
    model = _flat_text(embedding_model)
    space_id = _flat_text(embedding_space_id or getattr(embedder, "space_id", ""))
    if not dataset.is_dir():
        raise FileNotFoundError(dataset)
    if destination == dataset or dataset in destination.parents:
        raise ValueError("preference index must remain outside the Arena source dataset")
    if not model or not space_id:
        raise ValueError("embedding_model and embedding_space_id are required")
    if batch_size < 1 or (limit is not None and limit < 1):
        raise ValueError("batch_size and limit must be positive")
    destination.parent.mkdir(parents=True, exist_ok=True)

    with _BuildLock(destination):
        _validate_destination(destination)
        if destination.exists():
            current = _read_snapshot(destination, validate_artifacts=True)
            incompatible = (
                current.get("schema_version") != INDEX_SCHEMA_VERSION
                or current.get("embedding_text_policy") != EMBEDDING_TEXT_POLICY
                or current.get("preference_text_policy") != PREFERENCE_TEXT_POLICY
                or current.get("embedding_model") != model
                or current.get("embedding_space_id") != space_id
            )
            if incompatible and not rebuild:
                raise ValueError("existing preference index is incompatible; rebuild it")
            return await _build_generation(
                dataset,
                destination,
                embedder=embedder,
                embedding_model=model,
                embedding_space_id=space_id,
                batch_size=batch_size,
                limit=limit,
                progress=progress,
                rebuild=rebuild,
            )

        temporary = Path(
            tempfile.mkdtemp(dir=destination.parent, prefix=f".{destination.name}.building-")
        )
        try:
            result = await _build_generation(
                dataset,
                temporary,
                embedder=embedder,
                embedding_model=model,
                embedding_space_id=space_id,
                batch_size=batch_size,
                limit=limit,
                progress=progress,
                rebuild=rebuild,
            )
            os.replace(temporary, destination)
            return {**result, "index_path": str(destination)}
        finally:
            if temporary.exists() and not temporary.is_symlink():
                shutil.rmtree(temporary)


async def _build_generation(
    dataset: Path,
    destination: Path,
    *,
    embedder: Embedder,
    embedding_model: str,
    embedding_space_id: str,
    batch_size: int,
    limit: int | None,
    progress: Callable[[dict[str, Any]], None] | None,
    rebuild: bool,
) -> dict[str, Any]:
    started = time.monotonic()
    records, parse_report = await asyncio.to_thread(
        parse_arena_dataset, dataset, limit=limit
    )
    if not records:
        raise ValueError("Arena dataset produced no unambiguous preference pairs")
    generation_name = f"gen-{uuid.uuid4().hex}"
    generations = destination / _GENERATIONS_DIR
    if generations.exists() and (generations.is_symlink() or not generations.is_dir()):
        raise ValueError("preference-memory generations path is unsafe")
    generations.mkdir(parents=True, exist_ok=True)
    building = generations / f".building-{uuid.uuid4().hex}"
    final = generations / generation_name
    building.mkdir()
    vector_path = building / _VECTOR_FILE
    papers_path = building / _PAPERS_FILE
    preferences_path = building / _PREFERENCES_FILE
    indexed = 0
    expected_dimension: int | None = None
    faiss_index: Any | None = None
    content_digest = hashlib.sha256()

    try:
        faiss, numpy = _faiss_modules()
        with (
            papers_path.open("x", encoding="utf-8", newline="\n") as papers_handle,
            preferences_path.open("xb") as preferences_handle,
        ):
            for batch_start in range(0, len(records), batch_size):
                batch = records[batch_start : batch_start + batch_size]
                try:
                    vectors = await embedder([str(row["retrieval_text"]) for row in batch])
                except Exception as exc:  # noqa: BLE001
                    raise RuntimeError(
                        f"embedding batch failed: {_safe_embedding_error(exc)}"
                    ) from None
                if len(vectors) != len(batch):
                    raise ValueError("embedding provider returned the wrong row count")
                normalized = [_normalize_vector(vector) for vector in vectors]
                dimensions = {len(vector) for vector in normalized}
                if len(dimensions) != 1:
                    raise ValueError("embedding provider returned inconsistent dimensions")
                dimension = dimensions.pop()
                if expected_dimension is None:
                    expected_dimension = dimension
                    faiss_index = faiss.IndexIDMap2(faiss.IndexFlatIP(dimension))
                elif dimension != expected_dimension:
                    raise ValueError("embedding dimension changed during the build")
                matrix = numpy.ascontiguousarray(normalized, dtype="float32")
                ids = numpy.arange(indexed, indexed + len(batch), dtype="int64")
                faiss_index.add_with_ids(matrix, ids)
                for offset, record in enumerate(batch):
                    faiss_id = indexed + offset
                    blob = bytes(record["preferences_blob"])
                    packet_offset = preferences_handle.tell()
                    preferences_handle.write(blob)
                    embedding_bytes = matrix[offset].tobytes()
                    stored = {
                        "faiss_id": faiss_id,
                        "paper_id": record["paper_id"],
                        "title": record["title"],
                        "normalized_title": record["normalized_title"],
                        "abstract": record["abstract"],
                        "retrieval_hash": record["retrieval_hash"],
                        "paper_relative_path": record["paper_relative_path"],
                        "paper_sha256": record["paper_sha256"],
                        "pair_count": record["pair_count"],
                        "preferences_offset": packet_offset,
                        "preferences_length": len(blob),
                        "preferences_blob_sha256": _sha256_bytes(blob),
                        "preferences_raw_bytes": record["preferences_raw_bytes"],
                        "preferences_raw_sha256": record["preferences_raw_sha256"],
                        "embedding_sha256": _sha256_bytes(embedding_bytes),
                    }
                    papers_handle.write(
                        json.dumps(
                            stored,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
                    content_digest.update(
                        (
                            f"{stored['paper_id']}\0{stored['retrieval_hash']}\0"
                            f"{stored['paper_sha256']}\0{stored['preferences_raw_sha256']}\0"
                            f"{stored['embedding_sha256']}\n"
                        ).encode()
                    )
                indexed += len(batch)
                if progress is not None:
                    progress(
                        {
                            "papers_embedded": indexed,
                            "paper_count": len(records),
                            "preference_pairs": parse_report["preference_pair_count"],
                        }
                    )
            papers_handle.flush()
            preferences_handle.flush()
            os.fsync(papers_handle.fileno())
            os.fsync(preferences_handle.fileno())

        if expected_dimension is None or faiss_index is None:
            raise ValueError("Arena dataset produced no embeddings")
        faiss.write_index(faiss_index, str(vector_path))
        corpus_sha256 = content_digest.hexdigest()
        fingerprint = _sha256_bytes(
            (
                f"{INDEX_SCHEMA_VERSION}:{corpus_sha256}:{embedding_model}:"
                f"{embedding_space_id}:{EMBEDDING_TEXT_POLICY}:"
                f"{PREFERENCE_TEXT_POLICY}:{expected_dimension}:{indexed}:"
                f"{parse_report['preference_pair_count']}"
            ).encode()
        )
        artifacts = {
            filename: {
                "bytes": (building / filename).stat().st_size,
                "sha256": _sha256_file(building / filename),
            }
            for filename in (_VECTOR_FILE, _PAPERS_FILE, _PREFERENCES_FILE)
        }
        header = {
            "index_owner": INDEX_OWNER,
            "schema_version": INDEX_SCHEMA_VERSION,
            "status": "ready",
            "active_generation": generation_name,
            "index_format": _INDEX_FORMAT,
            "similarity": "cosine",
            "vectors_normalized": True,
            "embedding_model": embedding_model,
            "embedding_space_id": embedding_space_id,
            "embedding_dimension": expected_dimension,
            "embedding_text_policy": EMBEDDING_TEXT_POLICY,
            "preference_text_policy": PREFERENCE_TEXT_POLICY,
            "corpus_paper_count": indexed,
            "corpus_preference_pair_count": parse_report["preference_pair_count"],
            "corpus_content_sha256": corpus_sha256,
            "index_fingerprint": fingerprint,
            "completed_at_unix": int(time.time()),
            "parse_report": parse_report,
            "artifacts": artifacts,
        }
        candidate = {
            **header,
            "_artifact_paths": {
                filename: building / filename
                for filename in (_VECTOR_FILE, _PAPERS_FILE, _PREFERENCES_FILE)
            },
        }
        loaded = _load_records(candidate)
        _load_faiss_index(candidate, expected_records=len(loaded))
        os.replace(building, final)
        _atomic_write_json(destination / _INDEX_HEADER, header)
        _read_snapshot(destination, validate_artifacts=True)
    finally:
        if building.exists() and not building.is_symlink():
            shutil.rmtree(building)

    return {
        "status": "ok",
        "index_path": str(destination),
        "active_generation": generation_name,
        "schema_version": INDEX_SCHEMA_VERSION,
        "retrieval_mode": "faiss",
        "embedding_model": embedding_model,
        "embedding_space_id": embedding_space_id,
        "embedding_dimension": expected_dimension,
        "paper_count": indexed,
        "preference_pair_count": parse_report["preference_pair_count"],
        "index_fingerprint": fingerprint,
        "parse_report": parse_report,
        "rebuild_requested": bool(rebuild),
        "artifacts": artifacts,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def inspect_preference_index(index_path: str | Path) -> dict[str, Any]:
    """Return integrity-checked metadata without private response bodies."""

    raw = Path(index_path).expanduser()
    if raw.is_symlink():
        return {"status": "invalid", "index_path": str(raw), "error": "symlink path"}
    path = raw.resolve()
    if not path.exists():
        return {"status": "missing", "index_path": str(path)}
    try:
        snapshot = _read_snapshot(path, validate_artifacts=True)
        records = _load_records(snapshot)
        _load_faiss_index(snapshot, expected_records=len(records))
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return {"status": "invalid", "index_path": str(path), "error": _flat_text(exc)}
    return {
        "status": "ready",
        "index_path": str(path),
        "schema_version": snapshot["schema_version"],
        "active_generation": snapshot["active_generation"],
        "index_format": snapshot["index_format"],
        "retrieval_mode": "faiss",
        "embedding_model": snapshot["embedding_model"],
        "embedding_space_id": snapshot["embedding_space_id"],
        "embedding_dimension": snapshot["embedding_dimension"],
        "embedding_text_policy": snapshot["embedding_text_policy"],
        "preference_text_policy": snapshot["preference_text_policy"],
        "paper_count": len(records),
        "preference_pair_count": sum(int(row["pair_count"]) for row in records),
        "title_only_paper_count": int(
            (snapshot.get("parse_report") or {}).get("title_only_paper_count", 0)
        ),
        "index_fingerprint": snapshot["index_fingerprint"],
        "artifacts": dict(snapshot["artifacts"]),
    }


def _decompress_packet(blob: bytes, record: dict[str, Any]) -> list[dict[str, Any]]:
    expected_bytes = int(record.get("preferences_raw_bytes", -1))
    if expected_bytes < 0 or expected_bytes > MAX_PREFERENCE_PACKET_BYTES:
        raise ValueError("preference packet declares an unsafe size")
    decompressor = zlib.decompressobj()
    raw = decompressor.decompress(blob, MAX_PREFERENCE_PACKET_BYTES + 1)
    raw += decompressor.flush(MAX_PREFERENCE_PACKET_BYTES + 1 - len(raw))
    if (
        len(raw) != expected_bytes
        or not decompressor.eof
        or decompressor.unused_data
        or decompressor.unconsumed_tail
        or _sha256_bytes(raw) != record.get("preferences_raw_sha256")
    ):
        raise ValueError("preference packet failed its integrity check")
    value = json.loads(raw)
    if not isinstance(value, list) or len(value) != int(record["pair_count"]):
        raise ValueError("preference packet has an invalid pair count")
    for pair in value:
        if (
            not isinstance(pair, dict)
            or not _clean_text(pair.get("preferred_review"))
            or not _clean_text(pair.get("less_preferred_review"))
        ):
            raise ValueError("preference packet contains a malformed pair")
    return value


def _retrieve_packets(
    path: Path,
    *,
    query_vector: list[float],
    expected_fingerprint: str,
    requested_k: int,
    excluded_title: str,
    excluded_hash: str,
) -> dict[str, Any]:
    snapshot = _read_snapshot(path, validate_artifacts=True)
    if snapshot.get("index_fingerprint") != expected_fingerprint:
        raise ValueError("preference-memory generation changed during retrieval")
    records = _load_records(snapshot)
    index = _load_faiss_index(snapshot, expected_records=len(records))
    _faiss, numpy = _faiss_modules()
    normalized = _normalize_vector(query_vector)
    dimension = int(snapshot["embedding_dimension"])
    if len(normalized) != dimension:
        raise ValueError(
            f"query embedding dimension {len(normalized)} does not match index dimension {dimension}"
        )
    query = numpy.ascontiguousarray([normalized], dtype="float32")
    scores, ids = index.search(query, min(len(records), max(64, requested_k * 4)))
    packets: list[dict[str, Any]] = []
    pack_path = snapshot["_artifact_paths"][_PREFERENCES_FILE]
    with pack_path.open("rb") as handle:
        for score, raw_id in zip(scores[0].tolist(), ids[0].tolist(), strict=True):
            faiss_id = int(raw_id)
            if faiss_id < 0:
                continue
            if faiss_id >= len(records) or not math.isfinite(float(score)):
                raise ValueError("FAISS returned an invalid preference result")
            record = records[faiss_id]
            if (
                excluded_hash and record["retrieval_hash"] == excluded_hash
            ) or (
                excluded_title and record["normalized_title"] == excluded_title
            ):
                continue
            handle.seek(int(record["preferences_offset"]))
            blob = handle.read(int(record["preferences_length"]))
            if _sha256_bytes(blob) != record["preferences_blob_sha256"]:
                raise ValueError("compressed preference packet failed integrity")
            pairs = _decompress_packet(blob, record)
            # Deliberately omit source-paper, query, battle, and agent metadata.
            packets.append(
                {
                    "similarity": round(float(score), 6),
                    "preference_pairs": pairs,
                }
            )
            if len(packets) >= requested_k:
                break
    return {"snapshot": snapshot, "packets": packets}


def _unavailable(path: Path, *, code: str, warning: str) -> dict[str, Any]:
    embedding_problem = code == "preference_memory_embedding_unavailable"
    return {
        "status": "unavailable",
        "outcome": {"code": code},
        "index_path": str(path),
        "retrieval_mode": "none",
        "requested_top_k": DEFAULT_TOP_K,
        "matched_paper_count": 0,
        "matched_pair_count": 0,
        "matches": [],
        "warnings": [warning],
        "_preference_pairs": [],
        "setup_command": (
            "omni config embeddings --help"
            if embedding_problem
            else preference_index_setup_command(
                rebuild=code == "preference_memory_index_incompatible"
            )
        ),
        "next_actions": (
            ["Configure the embedding runtime used to build the preference index."]
            if embedding_problem
            else ["Build or rebuild the Arena preference FAISS index."]
        ),
    }


async def retrieve_preference_memory(
    index_path: str | Path,
    *,
    embedder: Embedder,
    structure: dict[str, Any],
    top_k: int = DEFAULT_TOP_K,
    embedding_model: str = "",
    embedding_space_id: str = "",
) -> dict[str, Any]:
    """Retrieve anonymous complete Arena pairs with exact FAISS cosine search."""

    raw = Path(index_path).expanduser()
    if raw.is_symlink():
        return _unavailable(
            raw,
            code="preference_memory_index_invalid",
            warning="Preference-memory index path must not be a symlink.",
        )
    path = raw.resolve()
    requested_k = max(1, min(int(top_k), MAX_TOP_K))
    if not path.is_dir():
        result = _unavailable(
            path,
            code="preference_memory_index_missing",
            warning="Arena preference memory was requested, but its index is missing.",
        )
        result["requested_top_k"] = requested_k
        return result
    title = _flat_text(structure.get("title"))
    abstract = _flat_text(structure.get("abstract"))
    if not title and not abstract:
        result = _unavailable(
            path,
            code="preference_memory_query_insufficient",
            warning="Preference retrieval needs a paper title or abstract.",
        )
        result["requested_top_k"] = requested_k
        return result
    try:
        header = await asyncio.to_thread(_read_snapshot, path, validate_artifacts=False)
    except (OSError, TypeError, ValueError):
        result = _unavailable(
            path,
            code="preference_memory_index_invalid",
            warning="Arena preference index could not be read or is not owned by Omni.",
        )
        result["requested_top_k"] = requested_k
        return result
    if (
        header.get("schema_version") != INDEX_SCHEMA_VERSION
        or header.get("embedding_text_policy") != EMBEDDING_TEXT_POLICY
        or header.get("preference_text_policy") != PREFERENCE_TEXT_POLICY
    ):
        result = _unavailable(
            path,
            code="preference_memory_index_incompatible",
            warning="Arena preference index is incompatible; rebuild it.",
        )
        result["requested_top_k"] = requested_k
        return result
    configured_space = _flat_text(
        embedding_space_id or getattr(embedder, "space_id", "")
    )
    configured_model = _flat_text(embedding_model)
    if not configured_space or configured_space != header.get("embedding_space_id"):
        result = _unavailable(
            path,
            code="preference_memory_embedding_unavailable",
            warning="Configured embedding space does not match the preference index.",
        )
        result["requested_top_k"] = requested_k
        return result
    if configured_model and configured_model != header.get("embedding_model"):
        result = _unavailable(
            path,
            code="preference_memory_embedding_unavailable",
            warning="Configured embedding model does not match the preference index.",
        )
        result["requested_top_k"] = requested_k
        return result

    query = paper_embedding_text(title, abstract)
    try:
        vectors = await embedder([query])
    except Exception as exc:  # noqa: BLE001 - never expose provider details
        result = _unavailable(
            path,
            code="preference_memory_embedding_unavailable",
            warning=(
                "Semantic Arena preference retrieval is unavailable: "
                f"{_safe_embedding_error(exc)}"
            ),
        )
        result["requested_top_k"] = requested_k
        return result
    try:
        if not isinstance(vectors, list) or len(vectors) != 1:
            raise ValueError("embedding provider did not return exactly one vector")
        raw_vector = vectors[0]
        if not isinstance(raw_vector, list):
            raise TypeError("embedding provider returned a malformed query vector")
        try:
            query_vector = [float(value) for value in raw_vector]
        except (TypeError, ValueError):
            raise ValueError(
                "embedding provider returned a non-numeric query vector"
            ) from None
        expected_dimension = int(header.get("embedding_dimension") or 0)
        if len(query_vector) != expected_dimension:
            raise ValueError(
                f"query embedding dimension {len(query_vector)} does not match "
                f"index dimension {expected_dimension}"
            )
    except (TypeError, ValueError) as exc:
        result = _unavailable(
            path,
            code="preference_memory_embedding_unavailable",
            warning=(
                "Semantic Arena preference retrieval is unavailable: "
                f"{_safe_embedding_error(exc, detail=True)}"
            ),
        )
        result["requested_top_k"] = requested_k
        return result
    try:
        retrieved = await asyncio.to_thread(
            _retrieve_packets,
            path,
            query_vector=query_vector,
            expected_fingerprint=str(header["index_fingerprint"]),
            requested_k=requested_k,
            excluded_title=_normalized_title(title),
            excluded_hash=_sha256_bytes(query.encode("utf-8")),
        )
    except RuntimeError as exc:
        result = _unavailable(
            path,
            code="preference_memory_faiss_unavailable",
            warning=_flat_text(exc),
        )
        result["requested_top_k"] = requested_k
        return result
    except (OSError, TypeError, ValueError, zlib.error, json.JSONDecodeError):
        result = _unavailable(
            path,
            code="preference_memory_index_invalid",
            warning="Arena preference FAISS index could not be validated or read.",
        )
        result["requested_top_k"] = requested_k
        return result

    packets = retrieved["packets"]
    snapshot = retrieved["snapshot"]
    pairs = [
        {"similarity": packet["similarity"], **pair}
        for packet in packets
        for pair in packet["preference_pairs"]
    ]
    pair_count = len(pairs)
    warnings: list[str] = []
    if len(packets) < requested_k:
        warnings.append(
            f"Arena preference retrieval returned {len(packets)} papers for top {requested_k}."
        )
    return {
        "status": "ok" if len(packets) == requested_k else "partial",
        "outcome": {
            "code": (
                "preference_memory_retrieved"
                if len(packets) == requested_k
                else "preference_memory_retrieved_with_limits"
            )
        },
        "index_path": str(path),
        "index_fingerprint": snapshot["index_fingerprint"],
        "active_generation": snapshot["active_generation"],
        "retrieval_mode": "faiss",
        "embedding_model": snapshot["embedding_model"],
        "embedding_space_id": snapshot["embedding_space_id"],
        "requested_top_k": requested_k,
        "matched_paper_count": len(packets),
        "matched_pair_count": pair_count,
        "matches": [
            {
                "rank": rank,
                "similarity": packet["similarity"],
                "preference_pair_count": len(packet["preference_pairs"]),
            }
            for rank, packet in enumerate(packets, 1)
        ],
        "warnings": warnings,
        "_preference_pairs": pairs,
        "use_boundary": (
            "Arena pairs show which complete feedback humans preferred for other "
            "papers. They may prompt an evidence-checked correction to the current "
            "formal review and guide helpful revision advice, but they are not proof or "
            "a score prior. Never copy their facts, ratings, verdicts, numbers, citations, "
            "or requested experiments into the current review."
        ),
    }


def public_preference_memory(result: dict[str, Any]) -> dict[str, Any]:
    """Remove complete response bodies from status/reporting output."""

    return {key: value for key, value in result.items() if key != "_preference_pairs"}


__all__ = [
    "DEFAULT_TOP_K",
    "EMBEDDING_TEXT_POLICY",
    "INDEX_OWNER",
    "INDEX_SCHEMA_VERSION",
    "MAX_TOP_K",
    "PREFERENCE_TEXT_POLICY",
    "build_preference_index",
    "inspect_preference_index",
    "paper_embedding_text",
    "parse_arena_dataset",
    "preference_index_setup_command",
    "public_preference_memory",
    "retrieve_preference_memory",
]
