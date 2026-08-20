# OmniScientist Web design QA

## Comparison target

- Source visual truth path: `/var/folders/tk/hz3zc2814lngkyjz9rnh4tqh0000gn/T/codex-clipboard-abdc6a96-7324-4e25-9ed6-37631014cad0.png`
- Implementation URL: `http://127.0.0.1:1088/`
- Final desktop screenshot: `/private/tmp/omni-web-qa/omni-web-desktop-1920x1080-final.png`
- Final tablet screenshots: `/private/tmp/omni-web-qa/omni-web-tablet-1024x768.png` and `/private/tmp/omni-web-qa/omni-web-tablet-panel-1024x768.png`
- Final mobile screenshot: `/private/tmp/omni-web-qa/omni-web-mobile-390x844-final.png`
- State: light theme; real local `omniscientist_v2` workspace; session `62c5c37b`; long assistant Markdown at the latest turn; inspector closed for the desktop comparison.

## Normalization

- Source pixels: 3840 x 2160. The source is a double-density desktop capture, normalized to a 1920 x 1080 CSS viewport at device scale factor 2.
- Implementation pixels: 1920 x 1080 at a 1920 x 1080 CSS viewport and device scale factor 1.
- Responsive evidence: 1024 x 768 at scale factor 1 and 390 x 844 at scale factor 1.
- The source includes browser chrome, an inactive overlay, watermarking, account/onboarding chrome, and different dynamic conversation content. Those regions were excluded from fidelity judgments; the app frame, sidebar, reading column, Markdown rhythm, user bubble, controls, and composer were compared.

## Full-view comparison evidence

The source and final implementation were opened together in one comparison input after density normalization. The final implementation preserves the source's primary proportions and hierarchy: a restrained 268 px navigation rail, a 748 px reading column, a 780 px centered composer, a 52 px top bar, plain assistant prose, right-aligned user bubbles, low-contrast metadata, and neutral light surfaces. Omni-specific task, artifact, ROM, notebook, and cost controls remain available without competing with the conversation.

## Focused comparison evidence

A separate crop was not needed because the normalized 1920 x 1080 comparison keeps the sidebar labels, 16 px Markdown typography, top controls, and composer controls legible. Focused responsive evidence was captured instead: the 1024 x 768 inspector overlay and the 390 x 844 conversation/composer view. Both preserve access to the core controls without horizontal clipping.

## Required fidelity surfaces

- Fonts and typography: system UI/CJK font stacks match the source's neutral product typography. Assistant copy is 16/28 px on desktop and 15/26 px on mobile; heading weights and margins create visible hierarchy without oversized display text. Long titles truncate; prose, links, and user text wrap safely.
- Spacing and layout rhythm: desktop sidebar, reading column, top bar, and composer proportions align with the reference. Assistant turns remain unboxed, while user messages use a single quiet bubble. Tablet and mobile switch navigation and inspectors to overlays rather than removing functionality.
- Colors and visual tokens: white main surface, cool-neutral sidebar, low-contrast borders, restrained shadows, and semantic accent/success/warning/danger tokens match the reference's visual intent. No gradients or decorative effects were introduced.
- Image quality and asset fidelity: the source has no required content imagery or illustration to reproduce. Interface glyphs use Lucide's consistent vector icon family. Local Markdown images retain aspect ratio, lazy-load, and stay within the reading column; remote images require an explicit user action before the browser contacts their host.
- Copy and content: navigation and system copy are concise Chinese labels appropriate to Omni. Dynamic conversation content is real local data rather than mock content. Background skill results and tool activity are collapsed into readable summaries while preserving expandable detail.
- Markdown: headings, paragraphs, nested lists, blockquotes, rules, GFM tables, task controls, links, inline code, fenced code, and images have dedicated layouts. Raw HTML is skipped; external links use `noopener noreferrer`; remote images are click-to-load; tables and code blocks scroll horizontally.
- Accessibility and interaction: semantic landmarks, labels, `aria-current`, pressed states, live status, focus rings, reduced-motion handling, modal focus restore/trap/Escape, and practical mobile overlays were verified. Open overlays make the background inert. Directory selection, workspace/session navigation, all five inspector tabs, and overlay close behavior were exercised with real data.

## Findings

No actionable P0, P1, or P2 findings remain.

- [P3] Formula typesetting and syntax highlighting are not included in this pass. Plain fenced code remains readable, copyable, and structurally correct. This is an optional research-content enhancement rather than a fidelity or usability blocker.

## Comparison history

### Iteration 1

- Earlier finding: [P2] At 390 px, the off-canvas sidebar was translated out of view but its controls could remain in the keyboard/accessibility traversal.
- Fix made: the closed mobile sidebar now becomes `visibility: hidden` after its exit transition and becomes visible immediately when opened. The real close button and Escape behavior remain available while open.
- Post-fix evidence: the final 390 x 844 screenshot shows the conversation and composer without overlap; the post-fix semantic snapshot no longer exposes the off-screen sidebar controls as actionable navigation. Desktop layout is unchanged in the final 1920 x 1080 comparison.

### Iteration 2

- Earlier finding: [P2] The directory dialog initially focuses its container; pressing Shift+Tab from that initial position could move focus behind the modal before the existing first/last-control loop applied.
- Fix made: the backward focus guard now treats the focused dialog container like the first control and wraps directly to the final action. Escape close and focus restoration are unchanged.
- Post-fix evidence: keyboard validation opens the real directory picker, wraps Shift+Tab from the dialog to the final `打开` action, and closes with Escape without exposing the background workflow.

### Iteration 3

- Independent review findings: [P1] approval could target another session's pending task; [P2] the brand control had an unrelated create-session action, conversation scroll-follow state could leak across sessions, a one-line unlabeled fenced block could be mistaken for inline code, remote Markdown images could make unsolicited third-party requests, mobile overlays lacked complete modal focus isolation, and two small-text tokens missed WCAG AA contrast.
- Fixes made: approval now receives the current session's exact pending task ID and validates its state; the brand is non-interactive; session changes reset follow state and move to the newest turn; block code is identified by its `<pre>` context without leaking parser props; remote images are explicitly click-to-load; navigation and inspector overlays trap/restore focus and make the conversation inert; supporting text tokens now meet a 4.5:1 contrast floor.
- Post-fix evidence: the 390 x 844 real session was used to open both overlays. Shift+Tab wrapped from each dialog container to its final focusable control, Escape closed it, and focus returned to the originating trigger. A server-rendered Markdown probe confirmed block code, inline code, prop filtering, remote-image pause, and raw-HTML skipping. The final source/implementation comparison was repeated after these fixes.
- Follow-up review findings: [P2] React 18 did not serialize a boolean `inert` attribute, protocol-relative image URLs could bypass the first remote-image check, and semantic blue/green status colors were just below 4.5:1 on their soft backgrounds.
- Follow-up fixes and evidence: `inert=""` is now serialized and was read back from the real browser on both navigation and inspector overlays; URL classification resolves against the current origin, so both absolute and protocol-relative third-party images pause before loading and protocol-relative links receive external-link protections; accent, success, warning, and danger text tokens now exceed 4.5:1 on their paired soft surfaces. The expanded server-render probe passed all seven checks, and desktop/mobile screenshots plus the normalized source comparison were recaptured after the fixes.
- Final independent re-review: no remaining P0, P1, or P2 findings; the reviewer independently reran TypeScript checking and the production build.

## Primary interactions and checks

- Opened a real workspace and historical CLI session.
- Rendered long Markdown with headings, nested lists, blockquotes, inline code, and a GFM table.
- Opened task, artifact, ROM, notebook, and cost inspectors.
- Opened the directory dialog and closed it with Escape; focus restoration and dialog semantics were present.
- Opened and closed mobile navigation; opened the mobile and tablet inspector overlays; verified focus trapping, inert background content, Escape, and trigger focus restoration.
- Checked the visible runtime during these interactions; no error banner, broken render, or unhandled interaction failure was observed. The current in-app browser adapter did not expose a direct console-message stream.
- `npm run build`: passed.
- `tsc --noEmit`: passed.
- `.venv/bin/pytest -q cli/tests/web`: 23 passed.
- Markdown/server-render probe: block code, inline code, DOM prop filtering, absolute and protocol-relative remote-image pause, protocol-relative external-link handling, raw-HTML skipping, and serialized `inert` passed.
- `pnpm audit --prod`: no known vulnerabilities.

## Implementation checklist

- [x] Match the professional light three-region shell and reading density.
- [x] Preserve all existing workspace, session, streaming, attachment, mode, approval, and inspector contracts.
- [x] Add complete Markdown presentation and safe overflow behavior.
- [x] Add desktop, tablet, and mobile navigation/inspector behavior.
- [x] Verify real workspace data, keyboard dialog behavior, production build, types, and Web API tests.

## Open questions

- None blocking. DeepSeek Harness design principles were independently implemented inside Omni's existing React/state architecture; DeepSeek branding and Cordis internals were intentionally not copied.

## Follow-up polish

- Optional: add syntax highlighting and KaTeX only when research sessions demonstrate a real need; neither should become a baseline bundle cost without usage evidence.

final result: passed

---

## Focused addendum — execution result and artifact references (2026-08-20)

### Evidence and normalization

- Source visual truth path: `/var/folders/tk/hz3zc2814lngkyjz9rnh4tqh0000gn/T/codex-clipboard-64572769-e3df-41b3-9b2b-441bc4279fc9.png`
- Browser-rendered implementation screenshot: `/private/tmp/omni-execution-result-redesign.png`
- Combined comparison input: `/private/tmp/omni-execution-result-comparison.png`
- Source pixels: 3238 x 1940; implementation pixels: 1280 x 720.
- Implementation viewport: 1280 x 720 CSS px at device scale factor 2. The source and implementation were aspect-fit into equal 1280 x 720 comparison frames without stretching.
- State: light theme; real `default` workspace; session `1bf1b0e8`; task `d316564f`; `scientific-figure` execution `f14ad134` expanded after completion.

### Full-view and focused comparison evidence

The source and implementation were viewed together in one 2560 x 720 comparison input. The source's raw 16 px / 28 px Markdown result dominated the execution inspector and repeated the background execution envelope, English `Artifacts:` marker, six long URI bullets, and CLI continuation commands. The implementation keeps the surrounding Task and Execution cards unchanged, while the result becomes a subordinate, compact section with a 12 px heading, a 12.5 px / 20 px summary, a bordered six-row artifact-reference group, 10–10.5 px monospaced metadata, and collapsed follow-up commands.

A separate crop was not required because the equal-frame comparison keeps both result regions legible. Browser inspection confirmed six structured artifact rows, no visible raw `Artifacts:` heading, a 9 px artifact-group radius, complete long-reference wrapping, and no console errors.

### Required fidelity surfaces

- Fonts and typography: result copy now follows the execution/activity density instead of the document Markdown scale; monospaced identifiers and paths use smaller optical sizing and wrap in full without depending on hover.
- Spacing and layout rhythm: the result heading mirrors the execution-process heading, artifact rows use a consistent 6 px vertical rhythm, and CLI-only continuation text is hidden behind one low-emphasis disclosure.
- Colors and visual tokens: all surfaces, borders, captions, and success feedback reuse existing theme tokens and remain compatible with dark mode.
- Image quality and asset fidelity: no content images are part of this component; the artifact heading reuses the existing Lucide-based Omni artifact icon.
- Copy and content: the redundant background envelope and English protocol marker are removed only in the Web projection; the summary, every artifact label/value, and all continuation commands remain available. Unknown result formats retain the original Markdown fallback.

### Findings and comparison history

No actionable P0, P1, or P2 visual findings remain.

- Earlier finding: [P1] execution results used the global 16 px / 28 px document Markdown scale and rendered persistence-protocol text as primary content.
- Fix made: added a tolerant Web-only result projection, compact result hierarchy, structured artifact references, and collapsed follow-up details without changing persisted content or agent behavior.
- Follow-up findings: [P2] rich Markdown headings/tables/code could still inherit document-scale typography; a green result icon implied success for failed executions; the 10 px artifact count missed the normal-text contrast target; and long references relied on hover-only truncation.
- Follow-up fixes: scoped rich Markdown typography—including background-result tables—to the compact result hierarchy, changed the result icon to a neutral artifact glyph, raised the count to an 11 px higher-contrast token, top-aligned multiline metadata, and allowed long references to wrap in full.
- Post-fix evidence: browser-rendered real execution, combined visual comparison, computed-style inspection, 129 component tests, and the production Vite build all passed.

### Primary interactions tested

- Selected the real `default` workspace and historical WeChat session.
- Expanded completed execution `f14ad134`.
- Verified the compact result summary, six artifact rows, and collapsed follow-up control.
- Checked browser console errors: none.

final result: passed
