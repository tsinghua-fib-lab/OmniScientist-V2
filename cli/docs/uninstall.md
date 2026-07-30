# Uninstalling OmniScientist

OmniScientist separates **program removal** from **research-data destruction**. The uninstaller
builds a read-only ownership plan first, renders it for review, and executes only after explicit
confirmation.

## Recommended commands

```bash
omni uninstall --dry-run
omni uninstall --yes
omni uninstall --purge --yes
omni uninstall --everything --yes
```

| Mode | Program | Daemons | Managed Skill exports and MCP | `OMNI_HOME` | In-place project `.omni` | Other detected installs |
|---|---:|---:|---:|---:|---:|---:|
| `--dry-run` | No | No | No | No | No | No |
| default | Yes | Yes | Yes | Preserved | Preserved | Current only |
| `--purge` | Yes | Yes | Yes | Deleted | Preserved | Current only |
| `--everything` | Yes | Yes | Yes | Deleted | Deleted when registered | Yes |

Use `--keep-program` to remove integrations or purge data without uninstalling the command. Use
`--json` for structured dry-run and execution output. `--all-project-data` requires `--purge`.

The default mode removes only the installation that is running the command. When another verified
Omni installation is present in `PATH` or the install manifest, the plan shows it as **preserved**
and points to `--all-installations`. Otherwise, after the current installation is removed, the
shell may resolve `omni` to that older installation. Use `--everything` when the goal is a single
full-machine Omni wipe rather than removal from one Python environment.

## What Omni owns

The planner inventories:

- active and recorded `uv tool`, `pipx`, conda/venv, or pip installations;
- the home service (its OS supervisor unit — launchd/systemd-user/Scheduled Task — is stopped and removed) plus any tracked/stale legacy per-workspace daemon pidfiles and detected `omni serve` processes;
- Skill copies recorded in `OMNI_HOME/skills_install.json`;
- Codex and Claude MCP entries named `omniscientist`;
- Omni shell-completion files;
- user data under `OMNI_HOME`;
- registered in-place project stores whose directory name is exactly `.omni`;
- known macOS Keychain accounts created by Feishu, DingTalk, or WeChat login.

Installer-based installs also record their method, executable, Python, source, and editable status
in `OMNI_HOME/install.json`. Manual installs remain removable because the running command and PATH
entry points are inspected at uninstall time.

## Safety invariants

- Dry-run never creates a data directory or changes a process, file, credential, or package.
- Default uninstall preserves research data.
- Data deletion is opt-in and refuses filesystem root, the user home, the current directory, an
  ancestor of the current directory, or a path that looks like a source repository.
- MCP removal deletes only the `omniscientist` entry and preserves all other servers/settings.
- Untracked external Skill copies are removed only by `--everything`, only when their complete tree
  byte-matches a current built-in, and only after a second identity check at execution time.
- Editable source checkouts are reported and preserved.
- External WeChat gateways, containers, model runtimes, Python itself, `uv`, `pipx`, Claude Code,
  Codex, and OpenClaw are not owned or removed.

## Cross-platform wrappers

macOS/Linux:

```bash
./cli/scripts/uninstall.sh --dry-run
./cli/scripts/uninstall.sh --everything --yes
```

Windows PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File cli\scripts\uninstall.ps1 -DryRun
powershell -ExecutionPolicy Bypass -File cli\scripts\uninstall.ps1 -Everything -Yes
```

On every platform, removal of the currently running package is scheduled in a detached process and
starts only after `omni uninstall` exits. This lets Typer and Rich finish rendering the report before
their own environment is removed. Windows uses PowerShell; macOS and Linux use a private temporary
POSIX shell script.

After an all-installations uninstall, start a fresh shell or refresh its command cache, then verify
that no other installation remains:

```bash
hash -r 2>/dev/null || true
which -a omni
```

## Recovery implications

Default uninstall is reversible by reinstalling the package; the prior `OMNI_HOME` remains. A purge
is intentionally irreversible unless the directory was backed up. Before a full wipe, inspect the
plan and archive any required artifacts or project stores:

```bash
omni uninstall --everything --dry-run --json
```
