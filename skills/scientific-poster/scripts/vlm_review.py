#!/usr/bin/env python3
"""Send one bound reference/candidate image pair to the configured Omni VLM."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))

from posterlib.runtime import runtime_io  # noqa: E402
from posterlib.visual import visual_review, vlm_client, vlm_review  # noqa: E402


async def run_request(
    request_path: str | Path,
    *,
    output_dir: str | Path,
    client: vlm_client.VlmClient,
) -> Path:
    """Review one request and write the bound model result."""

    request = visual_review.load_request(request_path)
    result = await vlm_review.review_request(request, client=client)
    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    result_path = destination / "model-result.json"
    runtime_io.write_json_atomic(
        result_path,
        result,
        indent=2,
        sort_keys=True,
    )
    return result_path


async def _invoke(args: argparse.Namespace) -> Path:
    request_path = Path(args.request).expanduser().resolve()
    output_dir = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir
        else request_path.parent
    )
    config = vlm_client.config_from_env(
        timeout_s=args.timeout,
        endpoint_override=args.endpoint,
        model_override=args.model,
    )
    if config is None:
        raise vlm_client.VlmError(
            "VLM endpoint, model, and API key are required via --endpoint/--model "
            "or OMNI_VLM_ENDPOINT, OMNI_VLM_MODEL, and OMNI_VLM_API_KEY"
        )
    client = vlm_client.VlmClient(config)
    return await run_request(
        request_path,
        output_dir=output_dir,
        client=client,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare a bound poster reference and candidate with the Omni VLM."
    )
    parser.add_argument(
        "--request",
        required=True,
        help="Path to visual-review-request.json produced by prepare-visual-review.",
    )
    parser.add_argument(
        "--output-dir",
        default="",
        help="Destination for model-result.json; defaults to the request directory.",
    )
    parser.add_argument("--endpoint", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args(argv)
    try:
        result_path = asyncio.run(_invoke(args))
    except (OSError, ValueError, vlm_client.VlmError) as exc:
        print(
            json.dumps(
                {"status": "error", "error": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {"status": "ok", "visual_review_result_path": str(result_path)},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
