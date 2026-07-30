# Changelog

This project follows [Keep a Changelog](https://keepachangelog.com/) and semantic versioning.

## Unreleased

### Added

- ``cli/scripts/release_selfcheck.sh`` recreates a GitHub Release cell on this
  machine: a minutes-long hot suite, an ubuntu/3.11 Docker cell, and
  ``--dispatch`` of Actions ``Release preflight`` for the Windows matrix.

## [2.0.0rc4] - 2026-08-17

### Added

- GitHub Actions ``Release preflight`` workflow: the release OS × Python
  matrix on demand, without a tag or PyPI publish.
- ``scripts/release.sh --preflight`` runs the same isolated ruff / pytest /
  release-gate / wheel / smoke cell as GitHub ``release.yml`` on this OS.

### Fixed

- Host slide fill passes ``artifact://`` (or an existing absolute path) into
  ``research-pptx``, not a store-relative ``artifacts/*.md``. A child whose
  cwd is not the control store no longer fails ``markdown_uri`` lookup.
- ``os_sandbox=auto`` without a kernel backend warns and keeps running, so
  stock Linux and GitHub runners are not fail-closed. ``cli_exec`` grants
  host-supplied input paths that do not open the control store.
- Windows release no longer fails specialist / workflow parallelism on a
  wall-clock budget or a triple-point overlap that process spawn can miss.
- Cancel persist retries a locked SQLite writer with the same queue as
  workflow checkpoints, so a dying Windows ``cli_exec`` does not turn
  ``react.tool.failed`` into ``database is locked``.
- Bundled ``scientific-poster`` seed PNGs are half-resolution palette images,
  so the published wheel is no longer dominated by ~10 MB of reference
  imagery.

### Changed

- Bumped the release candidate to `2.0.0rc4` (Git tag `v2.0.0rc4`).
  PyPI will not replace `2.0.0rc3`.

## [2.0.0rc3] - 2026-08-16

### Fixed

- A turn that already has the named files on this task no longer settles
  failed or Partial success because a leftover child failed or a host fill
  succeeded. WeChat hop 2 notifies the parent channel after the child
  finishes. The survey closer still retrieves after a ReAct demote.
  `综述` plus PPT binds slides, not a manuscript.
- Windows release no longer fails the async-subagent overlap test on
  wall-clock noise.
- WeChat no longer deadlocks when the model asks for `run_skill` in
  foreground: a turn that does not wait detaches the skill and still
  sends files on hop 2 after the inbound send lock drops.
- Scratch and `$OMNI_OUTPUT_DIR` now live outside the control store.
  Sandbox write roots never open `$OMNI_HOME`. `uninstall --purge` refuses
  a user store that was adopted as an in-place project, skips the outer
  store in the registered list, aborts if a service stop fails, and does
  not `rmtree` after a quarantine rename failure.

### Changed

- Bumped the release candidate to `2.0.0rc3` (Git tag `v2.0.0rc3`).
  PyPI will not replace `2.0.0rc2`.

## [2.0.0rc2] - 2026-08-01

### Added

- Added a reactive typed-plan control plane: immutable content-addressed revisions, contract-declared
  semantic constraints and binding provenance, one host-scoped model patch after findings, verified
  resolver ownership for factual identifiers, and a final live-contract execution gate.
- Added Codex-style busy input semantics (Enter steers, Tab queues, Esc stops), low-noise rendering
  for successful self-heals, and an evidence-backed release report with 100 production-pipeline
  accuracy cases, 10,000 independent finish/insert interleavings, a same-runner legacy/enforce
  latency comparison, and non-vacuous authority/replay/resolver, generic-wrapper policy,
  forged-constraint and host-rejection isolation, schema-invalid/domain-error/native-synthesis/
  durable-subtask output truth, failing-turn fallback, transient-ack recovery, and
  deterministic-workflow internal steer checks plus detached-command rejection with zero orphan
  controls. Separate real SQLite probes cover terminal requeue ownership, live-PID lease
  protection, dead-PID immediate recovery, and legacy lease-based at-least-once recovery of stale
  consumed controls. CI and tagged releases
  pre-create and publish the provenance-bound JSON report and fail closed when evidence is missing
  or a threshold regresses.
- Added a fail-closed 80% changed-code coverage artifact for `cli/src/omni/**/*.py` in pull-request,
  push, and tag workflows, with event-bound baselines, previous-release comparison, and an immutable
  first-release bootstrap.
- Added typed task recovery (`retry` / `resume` / `requeue`) with cross-workspace routing, plus
  branch-coverage tests that clear the first-tag changed-code coverage gate (≥80%).

### Changed

- Bumped the release candidate to `2.0.0rc2` (planned Git tag `v2.0.0rc2`).
- Plan approval now binds the canonical plan, skill catalog, provider contracts, and exact sensitive
  grants in one authority fingerprint and atomic claim. Reapproval replaces prior grants instead of
  retaining privilege by union.
- Built-in semantic fields publish compact planning hints and declarative detection signatures, so
  new contracts inherit planning, detection, repair, audit, and execution protection without a
  field-specific recovery rung.
- Generic skill wrappers are routing-only: concrete targets retain allow/block policy, owner hooks,
  approval, locking, and typed contracts while one logical call consumes one budget/event slot.
- Detached steering now rejects deterministic runners that have no semantic model boundary; the
  foreground composer transfers the same input to the next-turn queue instead of leaving an orphan.
- Provider authority now seals every regular file below the provider root and rejects symbolic or
  incomplete trees. Explicit retry/resume preserves the immutable root and appends a hash-linked
  reauthorization; ordinary drift still fails closed.
- Revision audit events use a final serialized 256 KiB budget, including UTF-8 and JSON escaping.
  Oversized history retains tamper-evident hashes and bounded provenance rather than claiming a
  reconstructable full snapshot.

### Fixed

- Prevented schema-valid semantic misbindings such as a generic figure for a RAG request, incorrect
  output language/layout/review mode, and syntactically valid but title-inconsistent arXiv ids from
  reaching execution silently.
- Fixed optimistic/duplicate plan validation events, mutable recovery history, stale or concurrent
  approval races, and accepted/persisted/executed plan divergence.
- Fixed native synthesis bypassing the typed gateway, explicit skill domain errors being recorded as
  successful tool calls, and delivered steering being duplicated when its durable acknowledgement
  briefly failed at turn settlement.
- Made final steering boundaries monotonic across late cost/audit/plan writes and enforced the open
  epoch inside the atomic SQLite control insert. Revocable one-shot gateway leases now prevent
  copied async contexts from retaining contract authority and bind delegated authority to one exact
  target.
- Made every control ownership transition atomic with its audit event; dead consumer processes are
  recoverable immediately while live consumers retain a lease.
- Repeated Esc or `/stop` now escalates from one cooperative cancellation request to a force-cancel
  for that foreground turn; completion starts the next turn with a fresh cancellation sequence.
- Made execution schemas immutable for one gateway call and resolved them through a scope-aware,
  deny-retrieval registry, closing Pointer-trampoline network/file access and late `$id`/anchor
  failures. Output validation now precedes the post-tool hook, and only host-sealed rejections retain
  pre-execution authority through generic wrappers.
- Persist Bash process outcomes as versioned, machine-readable command results while preserving the
  existing model observation. Non-zero exits remain completed tool invocations but now expose
  `command_status`, `reason`, and `exit_code` to task inspection, recovery, evaluation, and
  automation; bounded output keeps those fields intact for large or heavily escaped command output.

## [2.0.0rc1] - 2026-07-29

### Changed

- Declared the current product generation as OmniScientist V2 and aligned source/package/citation
  metadata on the `2.0.0rc1` release candidate with planned Git tag `v2.0.0rc1`. Publication to
  PyPI happens through the release workflow (`cli/scripts/release.sh`); until that runs the package
  is not represented as available on a public index.
- Documented this repository as the official next-generation implementation of the OmniScientist
  framework introduced in arXiv:2511.16931.
- Made each built-in skill a license-complete standalone distribution with `LICENSE.txt` and
  `NOTICE.md`; export reconciliation now detects missing or changed legal files.
- Replaced the inaccurate iLink independent-origin claim with explicit MIT-source adaptation provenance,
  retained experimental/non-endorsed warnings, and removed iLink from the REPL quickstart.
- Prepared distribution metadata, security boundaries, third-party notices, governance, and
  privacy documentation for public release.
- External skills now enter quarantine and require explicit trust before execution.
- WeChat iLink is experimental and no longer the default integration path.

### Removed

- Removed the non-redistributable Anthropic-derived PDF instruction skill.
