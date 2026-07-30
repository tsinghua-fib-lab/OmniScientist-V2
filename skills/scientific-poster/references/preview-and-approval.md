# Preview and approval

Run deterministic commands from the Skill directory.

## Probe browser inspection

```bash
python3 scripts/check_environment.py
```

The probe never installs anything. Rendered inspection needs Playwright and Chromium. A missing dependency returns exact `install_argv`. Run installation only with user/environment authority, then probe again.

## Validate and preview

```bash
python3 scripts/run.py --json '{"action":"validate","html":"/absolute/path/poster.html","source":"/absolute/path/paper.txt"}'
python3 scripts/run.py --json '{"action":"preview","html":"/absolute/path/poster.html","source":"/absolute/path/paper.txt"}'
```

Execute `preview_argv` as an argument vector. The server binds to IPv4 loopback on a random port, serves only the poster and authenticated state endpoints, injects an ephemeral selection overlay, and atomically writes `selection-state.json`. Canonical HTML never changes.

## Approve exact HTML

After the user enters the exact returned phrase, pass the original PDF again so approval can reconstruct the same page-aware source and figure manifest even after a process restart:

```bash
python3 scripts/run.py --json '{"action":"approve","source_html_path":"/absolute/path/poster.html","source_html_sha256":"<html-sha256>","source_html_uri":"artifact://<id>","pdf_uri":"/absolute/path/paper.pdf","approved":true,"operator_confirmation":"<exact operator_confirmation returned by draft/revise>","session_id":"<host-session>","output_dir":"/absolute/path/approval"}'
```

For text-only authoring, pass exactly one of `source` or `source_text` plus the machine-owned prepared figure hashes. Approval rechecks grounding and reruns Chromium against the exact source bytes. The bundle is `approved/<bundle_sha256>/{poster.html,approval.json}`. Any byte change needs another approval. Return the bundle paths and hashes unchanged to a downstream artifact-conversion Skill when another format is requested.

## Boundary

This Skill does not create PPTX files, probe PowerPoint dependencies, or rasterize a poster into a slide. HTML-to-PPTX conversion is a separate capability with its own editability and fidelity contract.
