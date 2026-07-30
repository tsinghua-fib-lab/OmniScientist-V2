# Changelog

This project follows [Keep a Changelog](https://keepachangelog.com/) and semantic versioning.

## Unreleased

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

### Changed

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
