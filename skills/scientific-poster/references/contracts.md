# Runtime contracts

Public JSON fields, outcomes, issue codes, and warning codes use `snake_case`. Action names and reusable resource ids use verb-led `kebab-case`; HTML attributes and CSS properties retain their platform spelling.

## Static HTML gate

Static validation requires a complete HTML5 document, one body-level poster root, physical millimetre page size, unique stable ids, source labels, and exactly five semantic regions. It rejects active HTML/CSS, remote references, unsafe SVG, unresolved figure tokens, placeholder copy, duplicate ids, and visible numbers absent from supplied source text.

The deterministic gate checks locators, literal numbers, and unsafe legal claims; it does not claim to prove semantic entailment. The authoring model remains responsible for selecting and paraphrasing evidence faithfully from the complete source.

## Paper-source gate

`pdf_uri` accepts one local PDF path or host artifact URI. Before authoring, the engine verifies the PDF signature, extracts page-aware text, and renders a bounded set of figures only when a nearby figure caption anchors the crop. Extracted captions and discussion context describe each `asset://N`; when any are available, the candidate must use at least one as an `img` source. The resolved image bytes are embedded into final HTML. Missing PyMuPDF returns a recoverable `missing_capability` result and no authoring model call is made.

## Chromium gate

Chromium measures the real rendered page and semantic regions. Approval is not ready when it finds clipping, masking, CSS filters, effective opacity below 10%, transparent text paint, scroll overflow, elements outside the poster, tiny viewing-distance type, blocked network requests, excessive unused page area, abandoned large method/evidence regions, stretched internal gaps, or display-scale hero text wrapping to four or more lines while using less than 70% of its parent content width. Empty canvas/picture containers and transparent, unpainted, or tiny SVG geometry do not count as content. A prepared PDF image counts as used only when its complete ancestor chain is unmasked and unclipped, it is fully inside the poster, and it occupies at least 0.5% of the page. Asymmetric figure-led pages are valid when evidence genuinely occupies the space.

If Playwright or Chromium is unavailable, return `missing_capability`; never pretend rendered QA passed.

## Selection state

The preview proxy injects its overlay only into the served response. `poster.html` bytes remain unchanged. A valid machine-owned selection stores:

- source HTML SHA-256;
- selected stable poster id and nearest semantic region;
- bounded text sample, rectangle, and computed styles;
- component id/version only when present in the selected DOM ancestry;
- capture timestamp.

The server rejects stale hashes and identities that do not exist in canonical HTML.

## Approval and handoff

The returned poster approval phrase is `APPROVE SCIENTIFIC-POSTER poster <html_sha256> source <grounding_source_sha256> figures <source_figure_manifest_sha256>`. Approval accepts exactly one grounding input. With `pdf_uri`, it deterministically reconstructs page-aware text and prepared-figure hashes; with text input, the caller supplies `source` or `source_text` and machine-owned figure hashes. Approval repeats static source grounding, prepared-figure use, and Chromium inspection against a private snapshot of the exact HTML bytes. The content-addressed bundle contains exactly `poster.html` and `approval.json`. The receipt owns no scientific or composition state.

The approval result returns the verified receipt path, exact HTML path, and content hashes for downstream handoff. This Skill performs no PPTX conversion and exposes no compatibility export action.
