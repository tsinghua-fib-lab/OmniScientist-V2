#!/usr/bin/env bash
# Build the loopback SPA into web/dist so hatch can ship it as omni/data/web.
# Release CI and scripts/release.sh run this before `uv build`. Ordinary
# `omni` commands never invoke it.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT/web"

DIST="$ROOT/web/dist"
STAGE="$(mktemp -d "$ROOT/web/.omni-web-dist.XXXXXX")"
BACKUP=""

cleanup() {
  local status=$?
  trap - EXIT
  if [[ -n "$BACKUP" && -e "$BACKUP" ]]; then
    if [[ ! -e "$DIST" ]]; then
      mv "$BACKUP" "$DIST"
    else
      rm -rf -- "$BACKUP"
    fi
  fi
  [[ -z "$STAGE" || ! -e "$STAGE" ]] || rm -rf -- "$STAGE"
  exit "$status"
}
trap cleanup EXIT

if [[ ! -f package.json ]]; then
  echo "web/package.json missing; cannot build the omni web UI" >&2
  exit 1
fi

# pnpm refuses to replace node_modules in a non-TTY unless CI is set.
if [[ ! -t 0 || ! -t 1 ]]; then
  export CI="${CI:-true}"
fi

if command -v pnpm >/dev/null 2>&1; then
  pnpm install --frozen-lockfile
  pnpm exec vite build --outDir "$STAGE"
elif command -v npm >/dev/null 2>&1; then
  npm install --no-package-lock
  npm exec -- vite build --outDir "$STAGE"
else
  echo "pnpm or npm is required to package the omni web UI" >&2
  exit 1
fi

if [[ ! -f "$STAGE/index.html" ]]; then
  echo "web/dist/index.html was not produced" >&2
  exit 1
fi

python3 -c '
import json, re, sys
from pathlib import Path
text = Path(sys.argv[1]).read_text(encoding="utf-8")
match = re.search(r"__version__\s*=\s*(?P<quote>[\x22\x27])([^\x22\x27]+)(?P=quote)", text)
if match is None:
    raise SystemExit("could not read OmniScientist version")
version = match.group(2)
Path(sys.argv[2]).write_text(
    json.dumps({"version": version}, indent=2) + "\n", encoding="utf-8"
)
print(f"stamped UI {version}")
' "$ROOT/cli/src/omni/__init__.py" "$STAGE/version.json"

if [[ -e "$DIST" ]]; then
  BACKUP="$(mktemp -d "$ROOT/web/.omni-web-backup.XXXXXX")"
  rmdir "$BACKUP"
  mv "$DIST" "$BACKUP"
fi
mv "$STAGE" "$DIST"
STAGE=""
if [[ -n "$BACKUP" ]]; then
  rm -rf -- "$BACKUP"
  BACKUP=""
fi

echo "built $ROOT/web/dist"
