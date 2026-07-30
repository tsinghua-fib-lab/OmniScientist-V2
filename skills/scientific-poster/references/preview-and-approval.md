# Preview and approval

Run deterministic commands from the Skill directory.

## Probe browser inspection

```bash
python3 scripts/check_environment.py
```

The default probe never installs anything. Rendered inspection needs Playwright and Chromium. A missing dependency returns exact `install_argv`. With user/environment authority, `python3 scripts/check_environment.py --install` runs only those allowlisted argv without a shell, stops on the first failure, and re-probes after success.

## Validate and preview

```bash
python3 scripts/run.py --json '{"action":"validate","html":"/absolute/path/poster.html","source":"/absolute/path/paper.txt"}'
python3 scripts/run.py --json '{"action":"preview","html":"/absolute/path/poster.html","source":"/absolute/path/paper.txt"}'
```

Model-backed `estimate` accepts the same source as `draft`. Portable `estimate` requires a complete prevalidated adaptive-module `content_budget`; source-only portable calls return `host_agent_required` rather than inventing an information structure.

For source-aware inspection, pass `paper_path`; the runner derives grounding text and expected figure hashes before Chromium. Inspection writes `poster.png`, enforces grounding/asset/safety/basic-reviewability boundaries, and records geometry, type, figure-scale, and layout measurements as visual-review feedback.

Execute `preview_argv` as an argument vector. The server binds to IPv4 loopback on a random port, serves only the poster and authenticated state endpoints, injects an ephemeral selection overlay, and atomically writes `selection-state.json`. Canonical HTML never changes.

## Review the rendered result

Every `draft` resolves one hash-bound, non-authoritative `design_reference` before HTML authoring, and `revise` retains that exact reference. Complete image-generation configuration produces a generated reference independently of VLM availability; missing, invalid, failed, or budget-ineligible generation selects the deterministic seed for the same content signals. A usable host-injected or environment VLM first interprets the exact reference pixels, then reviews every rendered candidate. The request binds the reference, full-resolution candidate, Chromium observations, and a small labeled evidence atlas. Space observations cover module-internal trailing space, vertical gaps between consecutive modules in the same visual lane, final lane-to-page trailing space, and the global page bottom; they remain descriptive evidence for VLM judgment rather than deterministic equal-height constraints. The VLM must assess every selected observation. A final pass requires deterministic integrity and the VLM rubric on the same bytes. The engine applies at most two combined revisions within the shared 540-second envelope and reviews staged candidates before activation.

The plan, resolved reference, and best accepted HTML are durable checkpoints. Rejected revised candidates stay diagnostic-only and cannot replace canonical HTML or live preview; their measured inspection failures remain in revision state for one bounded retry. A valid VLM revise/fail verdict blocks automatic PPTX export from the rejected candidate. With no configured VLM, the draft is `deterministic-only` and remains eligible for editable export after Chromium passes. A configured but unavailable or budget-exhausted VLM remains `vlm`/`awaiting-review`; it never fabricates a pass and blocks automatic export.

The provider-neutral fallback remains available. `draft` and `revise` return `visual_review_request_path`, the serialized `design_reference`, exact screenshot, and `inspection.visual_evidence`. For manual preparation, copy the complete reference and evidence values into the corresponding fields of a small JSON payload; neither may be reconstructed from hashes. Then run:

```bash
python3 scripts/run.py < /absolute/path/prepare-visual-review.json
python3 scripts/run.py --json '{"action":"submit-visual-review","visual_review_request":"/absolute/path/review/visual-review-request.json","visual_review_result":"/absolute/path/review/model-result.json","output_dir":"/absolute/path/review"}'
```

An image-capable harness may produce the result with any provider. The standalone adapter reads the same request and sends the reference, candidate overview, and optional evidence atlas in that order:

```bash
python3 scripts/vlm_review.py --request /absolute/path/review/visual-review-request.json
```

A `visual_revision_required` receipt may be passed as `visual_review_path` to `revise`. Its issue operations always instruct the complete-HTML authoring model: `restyle` and `reflow` preserve the scientific snapshot, while `content-replan` may curate or compress only named grounded explanatory modules under the stricter content-replan gate. It preserves the module's central takeaway and every retained value or qualifier, and cannot add facts, delete the module, or replace its figures or equations. `style-only` is available only for an explicit manual revision without a visual-review receipt and replaces CSS while preserving content and DOM order. At most two visual revisions are allowed.

## Approve exact HTML

After the user enters the exact returned phrase, pass the original PDF again so approval can reconstruct the same page-aware source and figure manifest even after a process restart:

```bash
python3 scripts/run.py --json '{"action":"approve","source_html_path":"/absolute/path/poster.html","source_html_sha256":"<html-sha256>","source_html_uri":"artifact://<id>","paper_path":"/absolute/path/paper.pdf","visual_review_path":"/absolute/path/review/visual-review.json","approved":true,"operator_confirmation":"<exact operator_confirmation returned by draft/revise>","session_id":"<host-session>","output_dir":"/absolute/path/approval"}'
```

For text-only authoring, pass exactly one of `source` or `source_text` plus the machine-owned prepared figure hashes. Approval rechecks grounding, the passing visual receipt, and Chromium against the exact source bytes. The bundle is `approved/<bundle_sha256>/{poster.html,visual-review.json,approval.json}`. Any byte change needs another visual review and approval. Use that exact approved HTML as the `export-pptx` input when both formats are delivered; the final pass, approval, and PPTX must share the same HTML bytes.

## Export editable PowerPoint

Run export on the exact inspected HTML. It does not require an approval receipt, but approval and export should use identical HTML bytes for a final handoff.

```bash
python3 scripts/run.py --json '{"action":"export-pptx","html":"/absolute/path/poster.html","source":"/absolute/path/paper.txt","output_dir":"/absolute/path/editable-poster"}'
```

The action returns native one-slide `poster.pptx`, `poster-scene.json`, and `poster-pptx-rubric.json`. Missing `python-pptx`, `mathml2omml`, or `PyMuPDF>=1.24` in the active Python environment returns `missing_capability` with scoped `install_argv`. `python3 scripts/check_environment.py --install` may install them into that active environment only with explicit authority; export itself never installs a dependency or writes into the Skill source tree.

## Boundary

HTML is the authoring source of truth. PPTX export preserves native text, tables, shapes, named objects, and semantic equations as editable Office Math, while scientific SVG or bitmap figures remain image objects. It does not promise arbitrary CSS fidelity, convert a full-page screenshot, or turn figure pixels into editable diagram primitives.
