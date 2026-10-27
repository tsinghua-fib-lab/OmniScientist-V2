#!/usr/bin/env bash
# scientific-figure renders SVG/PNG with the Graphviz `dot` binary.
# Release and CI runners do not ship it. Local machines that already have
# `dot` are left unchanged.
set -euo pipefail

if command -v dot >/dev/null 2>&1; then
  dot -V
  exit 0
fi

os="$(uname -s)"
case "$os" in
  Linux)
    sudo apt-get update
    sudo apt-get install -y graphviz
    ;;
  Darwin)
    brew install graphviz
    ;;
  MINGW*|MSYS*|CYGWIN*)
    choco install graphviz --no-progress -y
    if [[ -n "${GITHUB_PATH:-}" ]]; then
      echo 'C:\Program Files\Graphviz\bin' >> "$GITHUB_PATH"
    fi
    if [[ -x "/c/Program Files/Graphviz/bin/dot.exe" ]]; then
      "/c/Program Files/Graphviz/bin/dot.exe" -V
      exit 0
    fi
    ;;
  *)
    echo "error: cannot install Graphviz on ${os}" >&2
    exit 1
    ;;
esac

command -v dot >/dev/null 2>&1 || {
  echo "error: Graphviz install finished but dot is not on PATH" >&2
  exit 1
}
dot -V
