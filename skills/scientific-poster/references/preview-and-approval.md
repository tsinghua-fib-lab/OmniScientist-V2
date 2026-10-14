# Preview and approval

Run deterministic commands from the Skill directory.

## Probe runtime capabilities

```bash
python3 scripts/check_environment.py
```

The default probe never installs anything. Physical CSS page declarations need `tinycss2>=1.4,<2`; rendered inspection needs Playwright and Chromium. Run the probe with the same Python interpreter as the Skill. A missing dependency returns exact `install_argv`. With user/environment authority, `python3 scripts/check_environment.py --install` runs only those allowlisted argv without a shell, stops on the first failure, and re-probes after success. Omni also reads the declared parser requirement before executing the engine, so a missing parser is an environment setup issue, not an HTML repair request.

Omni installs the Python packages with the product and prepares Chromium during
install, `omni init`, and `omni update`. Run `omni skills setup scientific-poster`
to repair Chromium explicitly. Task execution never installs dependencies.

## Validate and preview

```bash
python3 scripts/run.py --json '{"action":"validate","html":"/absolute/path/poster.html"}'
python3 scripts/run.py --json '{"action":"preview","html":"/absolute/path/poster.html"}'
```

Model-backed `estimate` accepts the same source as `draft`. Portable `estimate` requires a complete prevalidated adaptive-module `content_budget`; source-only portable calls return `host_agent_required` rather than inventing an information structure.

Portable `prepare` exposes any structured equation shortcuts found during PDF preparation in
`paper_source.equations` and persists them in `poster-materials.json` under `paper.equations`.
Those shortcuts reduce transcription work; their absence does not disable mathematical authoring.

Inspection reads the self-contained poster HTML directly. It writes `poster.png`, verifies every selected figure already bound in the document, enforces safety/basic-reviewability boundaries, and records geometry, type, figure-scale, and layout measurements as visual-review feedback.

Execute `preview_argv` as an argument vector. The server binds to IPv4 loopback on a random port, serves only the poster and authenticated state endpoints, injects an ephemeral selection overlay, and atomically writes `selection-state.json`. Canonical HTML never changes.

## Review the rendered result

Every `draft` resolves one hash-bound, non-authoritative `design_reference` before HTML authoring, and `revise` retains that exact reference as provenance. Complete image-generation configuration produces a generated reference only when a usable VLM can interpret it; otherwise the skill selects one seed from content-shape signals. A usable host-injected or environment VLM observes the exact selected seed, custom, or generated pixels once during preflight, without target content shape or role weights. It records the source's actual regions, approximate proportions, hierarchy, rhythm, typography, surfaces, boundaries and figure treatment, including local exceptions and uncertainty. Source-image counts and positions are observations, not fixed target slots; the main author receives the target content and page separately and owns adaptation. If interpretation remains unavailable after its bounded transport retry, authoring continues without pixel-derived observations and the result remains explicitly unapproved; no code-authored layout or visual pass is substituted. The editor resolves static and geometry errors before returning a review candidate. The outer workflow reviews each geometry-valid candidate once and returns any visual feedback to the next editing session. Operational review uses the current screenshot, module index and physical measurements, without a plain-text transcript that could substitute for reading the image; it does not replay the plan prose. A geometry failure bypasses VLM review but does not prevent an unapproved partial draft export. A review request retains the brief only for provenance binding alongside the candidate HTML, full-resolution screenshot, Chromium observations, and a small labeled evidence atlas. Space observations remain descriptive evidence rather than deterministic equal-height constraints. A final pass requires deterministic integrity and the VLM rubric on the same bytes. A revise verdict returns hash-bound feedback to the shared revision loop, which stages and validates new HTML before activation.

The plan, resolved reference, and accepted HTML are durable checkpoints. When initial editing stops, its latest statically valid authored candidate is delivered as a partial draft with the actual inspection issues. A failed public revision preserves the existing published version. A geometry-valid revision can replace the current draft while awaiting visual acceptance. VLM review controls visual approval and supplies revision feedback; it does not act as a second delivery gate. A delivered statically valid draft remains eligible for an explicitly unapproved editable PPTX even when its rendered geometry needs revision. HTML and preview remain available if export fails. Model-backed drafting can continue without a usable VLM, but visual approval cannot. A configured but unavailable or budget-exhausted VLM remains `vlm`/`awaiting-review` and never fabricates a pass.

The provider-neutral fallback remains available. `inspect` writes one canonical `inspection.json` containing the exact screenshot, warnings, and visual evidence for the current HTML. Pass that file to `prepare-visual-review`; it derives the current module bindings, physical page, and rendered-inspection issues from the inspected HTML instead of asking the authoring agent to reconstruct them. When the user supplied a custom reference, pass its path as `reference_image`; it accompanies the candidate as a communication-quality comparison and supplies no scientific authority or mandatory layout.

```bash
python3 scripts/run.py --json '{"action":"inspect","html":"/absolute/path/poster.html","output_dir":"/absolute/path/inspection"}'
python3 scripts/run.py --json '{"action":"prepare-visual-review","html":"/absolute/path/poster.html","inspection_path":"/absolute/path/inspection/inspection.json","output_dir":"/absolute/path/review"}'
```

An independent image-capable harness may produce the result with any provider. An authoring agent must not manufacture its own passing result. With the standard VLM environment configured, run the standalone adapter; its default 1800-second timeout allows a local reasoning VLM to finish. The adapter validates the original candidate, screenshot, and evidence bindings, then sends a bounded whole-page overview and, only when the endpoint preserves independent images, a magnified source-figure atlas. The overview establishes hierarchy and placement; exact Chromium observations and any atlas support closer detail reading. Atlas magnification does not change the delivered physical scale, but needing close inspection alone is not a defect:

```bash
python3 scripts/review_poster.py --request /absolute/path/review/visual-review-request.json
```

Submit only that independent result. If no independent reviewer is configured, leave the request pending and label any HTML or PPTX as unapproved.

```bash
python3 scripts/run.py --json '{"action":"submit-visual-review","visual_review_request":"/absolute/path/review/visual-review-request.json","visual_review_result":"/absolute/path/review/model-result.json","output_dir":"/absolute/path/review"}'
```

A `visual_revision_required` receipt may be passed as `visual_review_path` to `revise`. For both restyle and reflow, the model chooses exact replacements, a complete stylesheet, or an atomic wrapper shell with references to the exact current title and module nodes; the host applies, validates, renders, and returns diagnostics against the resulting state. Intact modules may move or change span while every scientific snapshot and binding stays frozen. `content-replan` returns to the main scientific editor and may curate or compress only body text, ordinary detail points, and method-step details in named grounded modules under the stricter snapshot gate. Method-step labels and order, headings, takeaways, captions, tables, equations, identity, figures, and non-target copy remain frozen; the edit cannot add facts or delete the module. The automatic workflow compares each proposal with its incumbent in the same VLM call and promotes only `improved` results. An unchanged or regressed proposal leaves the incumbent selected and triggers a different-strategy edit while the shared deadline permits. Initial drafting owns global evidence selection; every rendered-candidate revision follows the same snapshot and targeted content-replan boundaries.

For a user-requested scientific correction, omit `visual_review_path` and pass `revision_mode: content-replan`, exact `content_replan_targets`, and the requested correction in `feedback`. The existing revision call reads the complete bound source and current HTML; it can correct a mistaken target heading or takeaway without treating the initial budget as truth. Figures, equations, identity, source bindings, and non-target scientific copy remain frozen. The revised HTML, not the archived initial budget, supplies the next review's copy. This is a requested edit, not automatic factual certification.

## Approve exact HTML

After the user enters the exact returned phrase, pass the original PDF again so approval can reconstruct the same page-aware source and figure manifest even after a process restart:

```bash
python3 scripts/run.py --json '{"action":"approve","source_html_path":"/absolute/path/poster.html","source_html_sha256":"<html-sha256>","source_html_uri":"artifact://<id>","paper_path":"/absolute/path/paper.pdf","visual_review_path":"/absolute/path/review/visual-review.json","approved":true,"operator_confirmation":"<exact operator_confirmation returned by draft/revise>","session_id":"<host-session>","output_dir":"/absolute/path/approval"}'
```

For paper input, approval accepts the same `paper_path`, `latex_project_dir` plus optional `main_tex`, or `arxiv_id` used for drafting and prepares the source again for exact binding. For text-only authoring, pass exactly one of `source` or `source_text` plus the machine-owned prepared figure manifest. Approval verifies the source-manifest hash, static HTML, and passing visual receipt against the exact same poster bytes; it does not rerun Chromium. The bundle is `approved/<bundle_sha256>/{poster.html,visual-review.json,approval.json}`. Any byte change needs another visual review and approval. Use that exact approved HTML as the `export-pptx` input when both formats are delivered; the final pass, approval, and PPTX must share the same HTML bytes.

## Export editable PowerPoint

Run export on the exact inspected HTML. It does not require an approval receipt, but approval and export should use identical HTML bytes for a final handoff.

```bash
python3 scripts/run.py --json '{"action":"export-pptx","html":"/absolute/path/poster.html","output_dir":"/absolute/path/editable-poster"}'
```

The action returns native one-slide `poster.pptx`, `poster-scene.json`, and `poster-pptx-rubric.json`. Missing `python-pptx`, `mathml2omml`, or the Skill's shared `PyMuPDF>=1.28` runtime in the active Python environment returns `missing_capability` with scoped `install_argv`. `python3 scripts/check_environment.py --install` may install them into that active environment only with explicit authority; export itself never installs a dependency or writes into the Skill source tree.

The scene records Chromium's resolved font faces; PPTX does not embed those fonts or
carry CSS fallback lists. A different Office or QA renderer may substitute a missing
face. Verify that renderer's access to the named fonts before attributing a typography
difference to authoring or export, and report any unresolved substitution.

## Boundary

HTML is the authoring source of truth. PPTX export preserves native text, tables, shapes, named objects, and semantic equations as editable Office Math, while scientific SVG or bitmap figures remain image objects. It does not promise arbitrary CSS fidelity, convert a full-page screenshot, or turn figure pixels into editable diagram primitives.
