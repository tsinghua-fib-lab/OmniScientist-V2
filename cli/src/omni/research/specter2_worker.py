#!/usr/bin/env python3
"""Offline JSONL worker for local SPECTER2 embeddings.

This module intentionally uses only the dedicated interpreter's installed
``torch``, ``transformers``, and ``adapters`` packages. It does not import Omni
and never downloads a model. Stdout is reserved for the JSONL protocol.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any

DIMENSION = 768
MAX_LENGTH = 512
MAX_BATCH_SIZE = 64
_PAPER_TEXT_RE = re.compile(
    r"^Title:\s*(?P<title>.*?)\s*\nAbstract:\s*(?P<abstract>.*)$",
    re.DOTALL,
)


def _write(payload: dict[str, Any]) -> None:
    sys.stdout.write(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    )
    sys.stdout.flush()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--base-model", required=True)
    parser.add_argument("--adapter", required=True)
    parser.add_argument("--device", default="cpu")
    return parser


def _load_runtime(base_model: str, adapter: str, device_name: str) -> tuple[Any, ...]:
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_DATASETS_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    base_path = Path(base_model).expanduser().resolve(strict=True)
    adapter_path = Path(adapter).expanduser().resolve(strict=True)
    if not base_path.is_dir() or not adapter_path.is_dir():
        raise RuntimeError

    # Some dependency versions print model-loading notices to stdout. Keep the
    # protocol clean by redirecting all third-party output to the discarded
    # stderr channel owned by the parent process.
    with contextlib.redirect_stdout(sys.stderr):
        import torch
        from adapters import AutoAdapterModel
        from transformers import AutoTokenizer

        if device_name.startswith("cuda") and not torch.cuda.is_available():
            raise RuntimeError
        device = torch.device(device_name)
        tokenizer = AutoTokenizer.from_pretrained(
            str(base_path),
            local_files_only=True,
        )
        model = AutoAdapterModel.from_pretrained(
            str(base_path),
            local_files_only=True,
        )
        adapter_name = model.load_adapter(
            str(adapter_path),
            load_as="specter2_proximity",
            set_active=False,
            local_files_only=True,
        )
        # Do not rely on load_adapter defaults: the retrieval adapter must be
        # the explicitly active inference adapter for every request.
        model.set_active_adapters(adapter_name)
        if int(getattr(model.config, "hidden_size", 0)) != DIMENSION:
            raise RuntimeError
        model.eval().to(device)
    return torch, tokenizer, model, device


def _specter_text(text: str, sep_token: str) -> str:
    match = _PAPER_TEXT_RE.match(text)
    if match is None:
        return text
    return f"{match.group('title').strip()}{sep_token}{match.group('abstract').strip()}"


def _embed(
    texts: list[str],
    *,
    torch: Any,
    tokenizer: Any,
    model: Any,
    device: Any,
) -> list[list[float]]:
    if not texts:
        return []
    if len(texts) > MAX_BATCH_SIZE or any(not isinstance(text, str) for text in texts):
        raise ValueError
    sep_token = str(tokenizer.sep_token or "[SEP]")
    inputs_text = [_specter_text(text, sep_token) for text in texts]
    with torch.inference_mode(), contextlib.redirect_stdout(sys.stderr):
        inputs = tokenizer(
            inputs_text,
            padding=True,
            truncation=True,
            max_length=MAX_LENGTH,
            return_tensors="pt",
            return_token_type_ids=False,
        )
        inputs = {name: value.to(device) for name, value in inputs.items()}
        output = model(**inputs).last_hidden_state[:, 0, :]
        rows = output.float().cpu().tolist()
    if len(rows) != len(texts):
        raise RuntimeError
    vectors: list[list[float]] = []
    for row in rows:
        vector = [float(value) for value in row]
        if len(vector) != DIMENSION or not all(math.isfinite(value) for value in vector):
            raise RuntimeError
        vectors.append(vector)
    return vectors


def main() -> int:
    try:
        args = _parser().parse_args()
        torch, tokenizer, model, device = _load_runtime(
            args.base_model,
            args.adapter,
            args.device,
        )
    except BaseException:  # never serialize dependency or local-path details
        _write(
            {
                "type": "error",
                "code": "worker_initialization_failed",
                "message": "local embedding worker could not initialize",
            }
        )
        return 1

    _write({"type": "ready", "dimension": DIMENSION})
    for raw_line in sys.stdin:
        try:
            request = json.loads(raw_line)
            if not isinstance(request, dict):
                raise ValueError
            request_type = request.get("type")
            if request_type == "close":
                return 0
            if request_type != "embed":
                raise ValueError
            request_id = request.get("id")
            if not isinstance(request_id, int):
                raise ValueError
            texts = request.get("texts")
            if not isinstance(texts, list):
                raise ValueError
            vectors = _embed(
                texts,
                torch=torch,
                tokenizer=tokenizer,
                model=model,
                device=device,
            )
            _write({"type": "result", "id": request_id, "vectors": vectors})
        except BaseException:  # responses must never echo an input or local path
            _write(
                {
                    "type": "error",
                    "code": "embedding_failed",
                    "message": "local SPECTER2 embedding failed",
                }
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
