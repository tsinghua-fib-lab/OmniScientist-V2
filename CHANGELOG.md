# Changelog

This project follows [Keep a Changelog](https://keepachangelog.com/) and semantic versioning.

## Unreleased

### Fixed

- LiveFigure generated-code cwd no longer sits under
  ``~/.omni/.../artifacts/livefigure-runs`` (the OS sandbox refuses writes
  there). The engine uses host ``$TMPDIR`` / ``ctx.scratch_dir``, then
  ``put_file`` into ``outputs/<title>_<task8>/``.
- ``$OMNI_OUTPUT_DIR`` harvest publishes into the current task bundle instead
  of registering ``artifacts/promoted/<task>/`` as the user-visible path
  (Codex imagegen: never leave a project-referenced asset only under
  ``$CODEX_HOME``).

### Changed

- Compute I/O is now a concrete environment, not a dollar token. Session
  context lists the absolute ``OMNI_OUTPUT_DIR`` / ``TMPDIR`` paths (Codex
  ``<environment_context>``). Sandboxed Python gets ``omni_io`` on
  ``PYTHONPATH``. A missing path that still contains a live ``$VAR`` is
  annotated, not rewritten. When ``required_outputs`` already bind a
  figure / slides / poster skill, ReAct receives that contract card and
  leftover bash that writes the same kind of file is steered back to
  ``run_skill``.

- The ReAct turn now treats a bound file with no admitted producer as
  unpayable: ask when that is the only remaining file debt, otherwise keep
  executing work that still has a consume path. A user-forbidden host
  service or skill is a this-turn admission fact. Lookup fuse counts only
  ledger tools (``get_task`` / ``memory_search`` / ``open_artifact``).
  ``bash`` / ``read_file`` / ``git`` of this workspace are progress, not
  inspect. A code review that names 对标 as a quality bar does not owe
  ``draft.manuscript``. ``task.inspect`` compiles only when the user asked
  for a prior task's status or location. Bash refuses writes into frozen
  Omni control stores with the same host-owned observation ``write_file``
  already uses.

- Figure routing is Codex-shaped: host facts only. An unspecified
  ``artifact.figure`` resolves to ``scientific-figure`` (SVG/PNG) whether
  or not a VLM is configured. ``livefigure`` runs when the user names it
  (``$livefigure``) or the plan binds ``figure.editable.pptx``. Host no
  longer classifies ``one slide`` / Graphviz / ``.dot`` in prose, no
  longer retries a sibling after an engine failure, and no longer
  injects a leftover ``.dot`` unless ``scientific-figure`` was explicit.
  Named livefigure without VLM stays unpayable / ``needs_input``.

- Walkthrough catalog launches use ``--out outputs_walkthrough`` (gitignored)
  instead of ``--out .``, so validation deliverables do not appear as
  untracked ``<title>_<task8>/`` folders next to ``cli/``. Product default
  remains ``outputs/``. ``omni --out`` help names that default.

- File tools share bash's Codex WorkspaceWrite *read* envelope: any
  path except sensitive files and frozen Omni control stores. Writes stay
  jailed. A bare absolute directory in the user message is a turn read
  root (same consent as ``@``), so ``list_dir(/other/repo)`` works
  instead of a bash crawl. ``grep`` of the filesystem root is refused.
  Distilled ``profile.md`` can no longer ban named tools
  (``write_file`` / ``bash`` / …). A 对标 / 仔细分析+源码 request binds
  ``draft.manuscript`` so settlement cannot succeed on a finding-ack.

- Process logs for CLI, ``omni serve``, and ``omni web`` share one
  single-line schema and rotate at 10 MiB, keeping 10 files total
  (``[observability] log_max_bytes`` / ``log_files``, or ``OMNI_LOG_*``).
  That matches kimi-code's size-based ``files`` count and Codex's
  component files under the home log dir. Foreground ``omni web`` stays
  on the terminal URL line; diagnostics go to
  ``~/.omni/logs/web-<project>.log``.

- New ``write_file`` / ``edit_file`` paths under leftover
  ``reports/`` / ``figures/`` / ``outputs/<other-task>/`` are rewritten
  into this task's ``outputs/<title>_<task8>/`` bundle (the 67f26c86
  layout). An existing leftover file is still edited in place. Source-tree
  directories (``src/``, ``docs/``) stay explicit.

- Generated files now land in one user-facing folder:
  ``outputs/<title>_<task8>/``. Task-bundle and filename rules are unchanged
  (``<title>_<task8>/`` and ``<slug>-<task8>-<art8>.ext``). Leftover
  ``reports/`` / ``figures/`` trees still resolve. ``outputs/`` and ``out/``
  are gitignored. ``artifact://<id>`` stays an internal SQLite handle for
  skills and ``resolve_path``; CLI and the Artifacts panel show the filesystem
  path (or nothing) instead of that URI.
- ``/config test`` and ``omni config test`` now live-probe the main model
  and a configured VLM / Semantic Scholar key, and name optional
  embeddings / VLM / S2 when they are unset. A VLM site origin or ``/v1``
  base URL is expanded to ``chat/completions`` (Claude Code
  ``ANTHROPIC_BASE_URL`` style); a complete path is left unchanged.
- A failed ``livefigure`` / ``paper-review`` no longer lets a
  ``write_file`` Markdown or harvested deck settle the parent Task as
  ``succeeded``. ``review`` is paid only by ``kind=review``; ``artifact.pptx``
  is the editable figure, not any ``.pptx``.
- ``$OMNI_OUTPUT_DIR`` harvest skips ``.venv`` / ``site-packages`` /
  LICENSE and only promotes scientific suffixes, so a bash fallback cannot
  register thousands of junk artifacts.
- ``paper-review`` treats ``arXiv 1706.03762`` as an identifier (fetches
  the PDF, or ``needs_input`` if it cannot). A DOI asks for a local file
  instead of ``Paper input does not exist: …``.

- ``react.tool.rejected`` now pairs a tool start. A known policy deny
  no longer replays ``unknown_outcome`` and steals the finish turn
  (afb9228d / 27803406).

- ``omni web`` Ctrl+C no longer dumps uvicorn's
  ``timeout graceful shutdown exceeded`` / ``Exception in ASGI application``
  traceback. Leftover SSE is cancelled as a normal stop.

- Task inspection no longer prints ``artifact://<id>`` under
  ``Result artifacts:``. The line is the filesystem path (the stored
  ``reports/`` / ``outputs/`` location when the file is gone). Cross-workspace
  ``get_task`` now resolves those checkout copies without a launch
  ``mirror_dir``.

- A stacked figure + manuscript + slides turn no longer dies after
  ``find_skill`` returns livefigure, scientific-figure, and research-pptx.
  Those cards (and an empty follow-up looking for a writing skill) are
  setup, not a hunt. The host steers to ``run_skill`` / ``write_file`` with
  tools still on. A real same-contract hunt still steers once; if a research
  feed still names unpaid files, one Codex-style tools-on replay runs before
  the fuse stops. The wrap-up no longer tells the model to call a tool after
  tools are disabled.
- Outputs list this ``task_id`` only. An identical twin stays a footnote
  (``/task show <id>``); its files are not shown as this turn's artifacts
  and do not hide an honest unpaid settlement.

- Schedule origin paths use the durable ``project_dir`` (``~/.omni/workspaces/…``)
  or an in-place ``<repo>/.omni``), not ``workspace_root``. Approving a
  proposal no longer creates ``sessions.sqlite3`` in the checkout. A legacy
  workspace-root value is re-keyed through ``get_paths``.

- ``research-pptx`` no longer treats a bare filename in ``topic`` as a
  cwd-required ``markdown_uri``. Mentions bind only when the file already
  exists as ``artifact://``, an absolute path, or a task outputs/reports
  deliverable. An explicit missing ``markdown_uri`` still fails with a
  retryable not-found error.

- Naming ``search_literature`` freezes that tool and keeps ``write_file``
  / ``run_skill`` closed for the retrieve window. Same-sentence figure /
  slides / manuscript names stay as unpaid debts. Only an explicit
  source-id-only scope stays retrieve-only. A bare mention
  (``explain search_literature …, do not run it``) is not a tool call.

- A skill that hits its own ``execution.max_seconds`` or ``stall_seconds``
  settles ``degraded`` when a durable artifact already exists, otherwise
  ``failed`` so a sealed single-skill turn can fall through. A workflow
  envelope expiry is ``failed`` even if a wrapper result exists. The
  message is not treated as a transient auto-retry.

- Contract-hunt pressure is the window after the last successful
  ``run_skill`` / ``run_workflow``. A later disjoint card can still hunt.
  ``recommended_next_actions`` are projected as observations; stop or
  ask is legal. ``resume --thread`` injects the typed brief and refuses
  an ambiguous hypothesis prefix. ``/new`` and a non-thread ``/resume``
  clear that brief. ``skills.default_for`` is consulted for the same
  capability slot only.

- Session lookup is exact-or-unique: a colliding prefix no longer
  resumes the newest match. A database error resolving a session
  principal does not fall back to owner memory or cache that guess.
  A dropped transcript write still finishes the turn, but marks it
  degraded. Named native tools are hard-routed only in protocol form
  (``name,`` / ``name(`` / ``name query=``), not ``调用`` / ``do not call``.
- Transcript and principal failures are turn-local and reach
  ``finish_turn`` as task ``degraded`` (one agent can run several
  inflight turns). A second compaction folds the live bridge so the
  first window is not dropped. Background memory / transcript / focus
  write-back skips an unresolved principal and goes through
  ``persist_message``. Mutating tools do not run if their start event
  cannot be recorded; unmatched starts pair by ``call_id``. A
  retrieve-only turn with zero hits does not fall back to model prose.
  Source / claim / run lookup is exact-or-unique; thread briefs filter
  claims and runs in SQL.

## [2.0.0rc6] - 2026-08-25

### Changed

- REPL ``/web`` starts the loopback UI in the background (``/web stop``,
  ``/web status``, ``/web port``). Shell ``omni web`` is still a foreground
  server stopped with Ctrl+C or Ctrl+D.
- Docs no longer claim ``omni project migrate`` or ``skills install`` /
  ``uninstall`` aliases; those commands are not on the CLI.
- ``omni eval blackbox`` fails CI when nothing is attempted or any attempt
  fails. Memory scenarios require a real model; ``offline_mock_smoke`` is the
  deterministic offline gate.
- Lone ``literature.search`` (and a named ``search_literature`` token) stay
  on native ReAct with a retrieve-only host tool policy. A written survey
  pair stays on capable ReAct with ``search_literature`` and ``write_file``;
  the model writes the manuscript. ``$skill`` protocol is unchanged.
- REPL welcome is one next step: ``/init`` or ``/model`` when the model is
  missing, otherwise ask a question with ``/web`` as the browser surface.
  The command manual moved to ``/help`` (conversation vs research). The
  footer advertises Shift+Enter only when xterm modified keys are ready;
  otherwise it keeps Ctrl+J. Slash completion still shows each command's
  description and lists session verbs first.
- Loopback SPA first JS no longer ships Settings or ``react-markdown``.
  Those load when settings open or a message needs rich markdown, so the
  entry chunk stays under Vite's 500 kB hint.
- Bumped the release candidate to `2.0.0rc6` (Git tag `v2.0.0rc6`).
  PyPI will not replace `2.0.0rc5`.

### Fixed

- A retrieve-only turn projects ``sources[].source_id`` instead of a title
  ``summary``. Settlement treats missing ``source_ids`` as an unpaid
  ``sources`` debt.
- Foreground skill drain no longer persists
  ``[Background skill execution completed]`` as a second assistant message.
  That line is only written when a detached notify channel is set.
- ``--mode plan`` skips ``ModelIntentPlanner.propose``, host-denies mutating
  and retrieval tools on the same turn, then pauses for approval.
- Local ``omni skills add`` uses the SKILL.md frontmatter name, not the
  folder stem.
- Non-TTY ``cli/scripts/build_web_ui.sh`` sets ``CI=true`` so pnpm can
  replace ``node_modules``.
- ``research-pptx`` stubs the unused ``image-size`` transitive (CVE-2025-71329
  / CVE-2025-71330 have no patched release). The renderer still passes
  explicit image width and height.

- Writing debts settle from this task's registered text document, not only
  ``kind=report`` / ``.md``. A ``write_file`` of a ledger token
  (``draft.section``) is rewritten to a human ``.md`` in the task reports
  bundle and no longer lands as an unclassified cwd file. Figure and slide
  debts stay type-strict. The host does not write the manuscript after the
  model stops. An unpaid named file is presented as degraded (continue the
  same task), not as succeeded. Host figure/slide fill runs only when this
  turn already has authored DOT or a registered manuscript.
- Host-known skill admission (missing VLM, binary, or Python module) is a
  route observation, not a turn-terminal ``needs_input``. The sealed skill
  still does not start; ReAct sees the setup command and can choose another
  catalog skill. Conversational confirms (``action: confirm_*``) still suspend.
- ReAct now lists skill names and descriptions (Codex-style budget) so
  ``run_skill`` can be called without hunting; empty ``find_skill`` after a
  card no longer trips the contract-hunt fuse, and ranking uses capability
  overlap instead of requiring every query word.
- In-flight model cancel still writes ``react.finished`` so the ReAct span
  closes (Python 3.11+ no longer drops the persist tail after ``Task.cancel()``).
  On Windows the event is queued like ``finish_task`` so a busy store cannot
  drop the closed span.
- Parent cancel settle is capped and memoized so a Linux busy store cannot
  hold a cancelled turn past the 8s workflow-cancel wait.
- Windows workflow-cancel now finishes after ``wait_for`` cancels the turn
  mid-settle. Advisory events yield the persist lock (and skip the busy
  queue once the execute task is already cancelling) so the checkpoint can
  land inside the 8s budget.

### Added

- ``cli/docs/user-walkthrough-cases.md`` (User Walkthrough Catalog) indexes
  116 user-facing cases with a specification table (named
  ``search_literature``, stacked prompts, long-horizon campaigns, and
  CLI/REPL) without advertising removed commands.
- ``cli/scripts/release_selfcheck.sh`` recreates a GitHub Release cell on this
  machine: a minutes-long hot suite, an ubuntu/3.11 Docker cell, and
  ``--dispatch`` of Actions ``Release preflight`` for the Windows matrix.
- ``release_selfcheck.sh --plan`` prints the ``release.yml`` job map
  (compatibility / build / smoke / publish) against the local stand-ins.
  ``--dispatch --wait`` blocks until the GitHub 9-cell preflight finishes.
  The hot list includes the in-flight model-cancel cell that fails on this OS.

## [2.0.0rc5] - 2026-08-20

### Fixed

- Vite's ``esbuild`` postinstall is explicitly allowed in ``web/pnpm-workspace.yaml``,
  so ``pnpm install --frozen-lockfile`` no longer fails on pnpm 11
  (``ERR_PNPM_IGNORED_BUILDS``) during ``omni web`` packaging.

### Changed

- Bumped the release candidate to `2.0.0rc5` (Git tag `v2.0.0rc5`).
  PyPI will not replace `2.0.0rc4`.

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
