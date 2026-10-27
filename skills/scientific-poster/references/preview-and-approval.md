# Preview and approval

Run deterministic commands from the Skill directory.

## Probe runtime capabilities

```bash
python3 scripts/check_environment.py
```

The default probe never installs anything. Physical CSS page declarations need `tinycss2>=1.4,<2`; rendered inspection needs Playwright and Chromium. Run the probe with the same Python interpreter as the Skill. A missing dependency returns exact `install_argv`. With user/environment authority, `python3 scripts/check_environment.py --install` runs only those allowlisted argv without a shell, stops on the first failure, and re-probes after success. Omni also reads the declared parser requirement before executing the engine, so a missing parser is an environment setup issue, not an HTML repair request.

The `visual-review` capability checks the local HTTP client, image processing and
JSON-response dependencies. It reports package readiness only. Endpoint credentials,
account availability and a passing visual judgment are separate requirements.

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

Every `draft` resolves one hash-bound `design_reference` before HTML authoring, and
`revise` retains its identity and saved design observations as provenance. A supplied custom image takes
precedence. Otherwise the skill selects the attributed academic reference for the
planned page orientation. Earlier saved reference identities remain restorable. Missing reference pixels leave the saved observations available; they do not invalidate the source checkpoint. A relocated bundled image is reused when its recorded identity still matches.

A usable host-injected or environment VLM observes the selected pixels once during
preflight. It records regions, approximate proportions, hierarchy, typography,
surfaces, boundaries and figure treatment, including uncertainty. The author uses
those observations with the paper, physical page and current design requirements.
When interpretation is unavailable, authoring continues from the grounded carrier
with visual approval pending.

The editor resolves static and geometry errors before returning a review candidate.
The outer workflow reviews each geometry-valid candidate once and returns visual
feedback to the next editing session. Review uses the current screenshot, module
index and physical measurements. The request binds the exact HTML, screenshot,
Chromium observations and evidence atlas. Geometry failures retain an explicitly
unapproved partial draft and its editable export. A final pass requires deterministic
integrity and the VLM rubric on the same bytes. Revision feedback remains hash-bound
to that candidate.

The plan, resolved reference, and accepted HTML are durable checkpoints. When initial editing stops, its latest statically valid authored candidate is delivered as a partial draft with the actual inspection issues. A failed public revision preserves the existing published version. A geometry-valid revision can replace the current draft while awaiting visual acceptance. VLM review controls visual approval and supplies revision feedback; it does not act as a second delivery gate. A delivered statically valid draft remains eligible for an explicitly unapproved editable PPTX even when its rendered geometry needs revision. HTML and preview remain available if export fails. Model-backed drafting can continue without a usable VLM, but visual approval cannot. A configured but unavailable or budget-exhausted VLM remains `vlm`/`awaiting-review` and never fabricates a pass.

The provider-neutral fallback remains available. `inspect` writes one canonical `inspection.json` containing the exact screenshot, warnings, and visual evidence for the current HTML. Pass that file to `prepare-visual-review`; it derives the current module bindings, physical page, and rendered-inspection issues from the inspected HTML instead of asking the authoring agent to reconstruct them. When the user supplied a custom reference, pass its path as `reference_image`; it accompanies the candidate as a communication-quality comparison and supplies no scientific authority or mandatory layout.

Pass the current design request as `user_direction` and structured choices as `visual_preferences`. The reviewer checks these bound requirements alongside the shared white-canvas and local-grouping principles. Open regions, light dividers and restrained complete outlines can coexist when they clarify group membership and the reading path. Review merged unrelated material, fragmented related material and boundaries that compete with the evidence. Explicit brand or custom-reference styling takes precedence within its requested scope. A readable poster with an unauthorized canvas tint, colored section cards or decorative single-side borders requires restyling.

```bash
python3 scripts/run.py --json '{"action":"inspect","html":"/absolute/path/poster.html","output_dir":"/absolute/path/inspection"}'
python3 scripts/run.py --json '{"action":"prepare-visual-review","html":"/absolute/path/poster.html","inspection_path":"/absolute/path/inspection/inspection.json","output_dir":"/absolute/path/review"}'
```

An independent image-capable harness may produce the result with any provider. An authoring agent must not manufacture its own passing result. With the standard VLM environment configured, run the standalone adapter; its optional `--timeout` sets the caller's request timeout. The host or caller owns the execution window. The adapter validates the original candidate, screenshot, and evidence bindings, then sends a bounded whole-page overview and, only when the endpoint preserves independent images, a magnified source-figure atlas. The overview establishes hierarchy and placement; exact Chromium observations and any atlas support closer detail reading. Atlas magnification does not change the delivered physical scale, but needing close inspection alone is not a defect:

```bash
python3 scripts/review_poster.py --request /absolute/path/review/visual-review-request.json
```

Submit only that independent result. If no independent reviewer is configured, leave the request pending and label any HTML or PPTX as unapproved.

```bash
python3 scripts/run.py --json '{"action":"submit-visual-review","visual_review_request":"/absolute/path/review/visual-review-request.json","visual_review_result":"/absolute/path/review/model-result.json","output_dir":"/absolute/path/review"}'
```

A visual_revision_required receipt supplies feedback to the selected HTML version. Automatic drafting sends every diagnosis to the same author with the complete paper, preserving joint copy and composition authority. The outer workflow compares the proposed and current screenshots, retaining the incumbent when the proposal regresses. Current user requests permit prose and layout edits within the existing content-replan boundary; explicit full-layout requests preserve current scientific copy and objects. A passing receipt supplies review context for a new user revision.

## Revise an existing candidate

For Omni `action: revise`, set `source_html_uri` to the selected result's `html_uri`,
or its local `html_path`. Keep the adjacent `<html-filename>.poster-state.json` when
moving a local candidate; it restores the complete source and saved design direction.
Optionally pass the returned `html_sha256` as `source_html_sha256` to pin that version.

Keep the current request in `input`; use `feedback` for revision goals and visible
defects. `visual_preferences` accepts the free-form strings `typography`, `framing`,
and `accent_color`, which merge with saved choices. A matching `visual_review_path`
supplies review diagnostics. For example:

```json
{
  "action": "revise",
  "input": "Improve body readability and clarify the method panel.",
  "source_html_uri": "/absolute/path/poster.html",
  "feedback": "Increase body text and separate adjacent formulas while preserving scientific content.",
  "revision_mode": "full-layout",
  "visual_preferences": {
    "typography": "Comfortable regular-weight body text",
    "framing": "Open groups with light dividers",
    "accent_color": "navy"
  }
}
```

Describe the requested change in `input` or `feedback`. Ordinary user revisions use
`content-replan`, allowing prose and layout to be edited together. Optional
`content_replan_targets` limits prose edits to named `data-poster-module` ids or
`poster` for unowned copy. Omitted targets allow all current prose. Figures, equations,
table data, identity and source bindings retain their existing scope.
Use explicit `full-layout` for composition-only changes. A receipt-only revision
follows its issue operations and copy targets. A new user request takes precedence
over the receipt's scope and starts a fresh review cycle.
A passing receipt by itself leaves the selected poster unchanged and returns
`missing_input` for the revision direction, before any author call.
Codex and Claude Code apply these edits with their host's file-edit tools, then use
the portable inspection, review and export actions above.

## Record optional acceptance

Draft and export already return usable artifacts. When the user explicitly accepts a selected version, record that decision using its HTML path and returned file identity:

```bash
python3 scripts/run.py --json '{"action":"approve","source_html_path":"/absolute/path/poster.html","source_html_sha256":"<html-sha256>","approved":true,"output_dir":"/absolute/path/acceptance"}'
```

Optionally include visual_review_path to preserve a matching review. Its original state remains passed or revision-required; without a review the acceptance record reports not-reviewed. The saved directory contains poster.html, approval.json and the optional visual-review.json. The source paper and a typed confirmation phrase are unnecessary. This action records acceptance of the chosen HTML version and leaves PPTX native-layout review separate.

## Export editable PowerPoint

Export freezes the validated HTML bytes before native capture. The returned source identity describes that snapshot. The caller's shared export deadline governs capture and rendering; a default subprocess timeout applies when no deadline was supplied.

```bash
python3 scripts/run.py --json '{"action":"export-pptx","html":"/absolute/path/poster.html","output_dir":"/absolute/path/editable-poster"}'
```

The action returns native one-slide `poster.pptx`, `poster-scene.json`, and `poster-pptx-rubric.json`. Missing `python-pptx`, `mathml2omml`, or the Skill's shared `PyMuPDF>=1.28` runtime in the active Python environment returns `missing_capability` with scoped `install_argv`. `python3 scripts/check_environment.py --install` may install them into that active environment only with explicit authority; export itself never installs a dependency or writes into the Skill source tree.

Structural export returns `status: ok`, outcome `pptx_export_complete`,
`pptx_sha256`, and `pptx_visual_quality_state: awaiting-review`. The existing HTML
receipt remains valid for its HTML and Chromium screenshot. Report the generated
PPTX together with its pending native review; successful export preserves the
overall HTML and workflow status. HTML inspection defects, unavailable visual review,
unfinished workflow stages and export failures retain their own partial or error
outcomes. Native layout acceptance requires inspecting the exact PPTX in Microsoft PowerPoint,
including equation proportions, script placement, text spacing and missing content.
The exported file remains available while this review is pending.

For ordinary text, the scene records Chromium's resolved font faces; PPTX does not embed those fonts or
carry CSS fallback lists. A different Office or QA renderer may substitute a missing
face. Verify that renderer's access to the named fonts before attributing a typography
difference to authoring or export, and report any unresolved substitution.
Microsoft PowerPoint supplies the native Office Math layout and editing behavior.
Other presentation applications remain outside the supported rendering target.

## Boundary

HTML is the authoring source of truth. PPTX export preserves native text, tables, shapes, named objects, and semantic equations as editable Office Math, while scientific SVG or bitmap figures remain image objects. It does not promise arbitrary CSS fidelity, convert a full-page screenshot, or turn figure pixels into editable diagram primitives.
