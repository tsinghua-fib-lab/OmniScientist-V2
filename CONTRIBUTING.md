# Contributing to OmniScientist V2

See [`cli/CONTRIBUTING.md`](cli/CONTRIBUTING.md) for development setup and code conventions.

## Before opening a change

- Keep tests offline and use the mock/ScriptedLLM providers.
- Run `.venv/bin/ruff check cli/src cli/tests` and `.venv/bin/pytest -q`.
- Add tests for behavior changes and update user-facing documentation.
- Do not commit credentials, personal research data, generated workspaces, or third-party material
  without a redistribution-compatible license and attribution.
- New bundled skills must declare `license`, `role`, capabilities, input/output contracts, and
  provenance under `metadata.helixforge`, and include standalone `LICENSE.txt` and `NOTICE.md`.

## Certificate of Origin

This project uses the [Developer Certificate of Origin 1.1](https://developercertificate.org/).
Sign every commit with `git commit -s` to certify that you have the right to submit it under
Apache-2.0. Contributions intentionally submitted without a separate written agreement are made
under Apache-2.0.

## Security and conduct

Do not report vulnerabilities in public issues. Follow [`SECURITY.md`](SECURITY.md). Participation
is governed by [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).
