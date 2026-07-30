#!/usr/bin/env bash
# OmniScientist uninstall wrapper for macOS and Linux.
#
# Usage:
#   scripts/uninstall.sh --dry-run
#   scripts/uninstall.sh --yes
#   scripts/uninstall.sh --everything --yes

set -euo pipefail

if ! command -v omni >/dev/null 2>&1; then
  echo "The omni command is not on PATH, so the ownership-aware uninstaller cannot run." >&2
  echo "Activate the environment that contains OmniScientist, then run: omni uninstall --dry-run" >&2
  exit 1
fi

exec omni uninstall "$@"
