"""Vector similarity helpers.

The default backend stores the embedding inline on each row and ranks candidates
with cosine similarity computed in Python — no native extension, more than
adequate for single-user scale. When the optional ``sqlite-vec`` extension is
installed it is slotted in *behind the same surface* (:func:`similarity_scores` /
:func:`rank_by_similarity`): the same candidate set is scored with the C-native
cosine distance instead of the Python hot loop, so results are identical and the
scan stops being a pure-Python bottleneck. Selection is governed by
``memory.vector_backend`` (``auto`` | ``sqlite_vec`` | ``none``); everything
degrades gracefully when the extension is absent.
"""

from __future__ import annotations

import math
import os

_VEC_TRIED = False
_VEC_MOD: object | None = None


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def sqlite_vec_available() -> bool:
    """Whether the optional ``sqlite-vec`` extension is importable + loadable."""
    return _sqlite_vec_module() is not None


def _sqlite_vec_module() -> object | None:
    global _VEC_TRIED, _VEC_MOD
    if _VEC_TRIED:
        return _VEC_MOD
    _VEC_TRIED = True
    if os.environ.get("OMNI_DISABLE_SQLITE_VEC"):
        _VEC_MOD = None
        return None
    try:
        import sqlite_vec

        _VEC_MOD = sqlite_vec
    except Exception:  # noqa: BLE001 — extension missing/unloadable → Python path
        _VEC_MOD = None
    return _VEC_MOD


def use_sqlite_vec(backend: str) -> bool:
    """Resolve ``memory.vector_backend`` to whether sqlite-vec should be used.

    ``auto`` uses it when installed; ``sqlite_vec`` requests it explicitly (still
    falls back if unavailable); ``none``/anything else stays on the Python path.
    """
    b = (backend or "auto").strip().lower()
    if b in ("none", "python", "off"):
        return False
    return sqlite_vec_available()


def _vec_similarity_scores(
    query_vec: list[float], candidates: list[tuple[str, list[float]]]
) -> dict[str, float]:
    """Cosine similarities via an in-memory sqlite-vec ``vec0`` KNN table.

    Builds an ephemeral index over the candidate vectors, so it never touches the
    persistent store or its connections and is safe to call per query. Raises on
    any sqlite-vec error so the caller can fall back to the Python path.
    """
    sqlite_vec = _sqlite_vec_module()
    if sqlite_vec is None:
        raise RuntimeError("sqlite-vec unavailable")
    import sqlite3

    dim = len(query_vec)
    rows: list[tuple[int, bytes]] = []
    ids: list[str] = []
    for cid, vec in candidates:
        if not vec or len(vec) != dim:
            continue
        ids.append(cid)
        rows.append((len(ids), sqlite_vec.serialize_float32([float(x) for x in vec])))
    if not rows:
        return {}
    con = sqlite3.connect(":memory:")
    try:
        con.enable_load_extension(True)
        sqlite_vec.load(con)
        con.enable_load_extension(False)
        con.execute(
            f"CREATE VIRTUAL TABLE v USING vec0(embedding float[{dim}] distance_metric=cosine)"
        )
        con.executemany("INSERT INTO v(rowid, embedding) VALUES (?, ?)", rows)
        q = sqlite_vec.serialize_float32([float(x) for x in query_vec])
        res = con.execute(
            "SELECT rowid, distance FROM v WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
            (q, len(ids)),
        ).fetchall()
    finally:
        con.close()
    # cosine distance = 1 - cosine similarity
    return {ids[rowid - 1]: 1.0 - float(dist) for rowid, dist in res}


def similarity_scores(
    query_vec: list[float],
    candidates: list[tuple[str, list[float]]],
    *,
    backend: str = "auto",
) -> dict[str, float]:
    """Cosine similarity of ``query_vec`` to each candidate, as ``{id: score}``.

    Uses sqlite-vec when selected + available, otherwise a pure-Python cosine
    scan. Candidates with an empty or mismatched-length vector are skipped.
    """
    if not query_vec:
        return {}
    if use_sqlite_vec(backend):
        try:
            return _vec_similarity_scores(query_vec, candidates)
        except Exception:  # noqa: BLE001 — any vec failure → Python fallback
            pass
    dim = len(query_vec)
    return {
        cid: cosine(query_vec, vec)
        for cid, vec in candidates
        if vec and len(vec) == dim
    }


def rank_by_similarity(
    query_vec: list[float],
    candidates: list[tuple[str, list[float]]],
    *,
    top_k: int = 10,
    backend: str = "auto",
) -> list[tuple[str, float]]:
    scored = list(similarity_scores(query_vec, candidates, backend=backend).items())
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k]


__all__ = [
    "cosine",
    "sqlite_vec_available",
    "use_sqlite_vec",
    "similarity_scores",
    "rank_by_similarity",
]
