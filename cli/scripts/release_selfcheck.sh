#!/usr/bin/env bash
# Local stand-in for GitHub ``release.yml`` / ``release-preflight.yml``.
#
# A tagged Release re-runs pytest on ubuntu/macOS/windows × 3.11/3.12/3.13.
# This machine can reproduce two of those cells; it cannot reproduce Windows.
#
#   scripts/release_selfcheck.sh              # this OS, hot tests (minutes)
#   scripts/release_selfcheck.sh --plan       # GitHub release.yml steps vs local
#   scripts/release_selfcheck.sh --linux      # GitHub ubuntu × 3.11 in Docker
#   scripts/release_selfcheck.sh --full       # this OS, same pytest as one
#                                             # release.yml compatibility cell
#   scripts/release_selfcheck.sh --linux --full
#   scripts/release_selfcheck.sh --dispatch   # real 9-cell matrix on GitHub
#   scripts/release_selfcheck.sh --dispatch --wait
#   scripts/release_selfcheck.sh --repeat 3   # rerun the selected suite
#
# Isolated venv (UV_PROJECT_ENVIRONMENT). Does not touch cli/.venv.
# Dirty trees are allowed — that is the point of checking before commit.
# Windows still requires Actions → Release preflight (or --dispatch).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_PATH="$SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
HOT_LIST="$SCRIPT_DIR/release_hot_tests.txt"
CANONICAL_REPOSITORY_SLUG="tsinghua-fib-lab/OmniScientist-V2"

LINUX=0
FULL=0
DISPATCH=0
WAIT=0
PLAN=0
REPEAT=1
RELEASE_PYTHON="${OMNI_RELEASE_PYTHON:-}"
DOCKER_PLATFORM="${OMNI_RELEASE_DOCKER_PLATFORM:-linux/amd64}"

print_plan() {
  cat <<'EOF'
GitHub Release (`release.yml`, triggered by tag v*)
  1. compatibility   ubuntu-latest|macos-latest|windows-latest × 3.11|3.12|3.13
                     (ubuntu-latest × 3.12 excluded; that cell is the coverage build)
       uv sync --project cli --all-extras
       uv run --project cli ruff check cli/src cli/tests
       uv run --project cli pytest -q -m "not release_gate"
  2. build           ubuntu-latest × 3.12
       tag == omni.__version__, Repository URL, ruff,
       reactive-binding + offline-corpus gates,
       coverage pytest -m "not release_gate", 80% changed-code,
       build_web_ui.sh, uv build, check_dist.py
  3. smoke           ubuntu-latest|macos-latest|windows-latest: isolated wheel + omni --version
  4. publish         PyPI Trusted Publisher (not simulated)

GitHub Release preflight (`release-preflight.yml`, no tag / no PyPI)
  jobs 1 + 2 gates on the same 9 OS×Python cells.

Local stand-in (this script; dirty tree OK)
  default / --hot     minutes: release_hot_tests.txt (Windows/Linux SQLite flakes)
  --full / --compat   job 1 + job 2 gates on this OS only (~45 min)
  --linux [--full]    job 1 on ubuntu × 3.11 in Docker
  --dispatch [--wait] job 1 all 9 cells + job 2 gates on GitHub (needs a pushed SHA)
  scripts/release.sh --preflight
                      job 1/2 on this OS + wheel + this-OS smoke

This Mac/Linux host cannot reproduce Windows aiosqlite locks.
Do not tag until --dispatch (or CI) is green on windows-latest.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --linux) LINUX=1; shift ;;
    --full|--compat) FULL=1; shift ;;
    --dispatch) DISPATCH=1; shift ;;
    --wait) WAIT=1; shift ;;
    --plan) PLAN=1; shift ;;
    --hot) FULL=0; shift ;;
    --repeat) REPEAT="${2:?--repeat needs a count}"; shift 2 ;;
    --python) RELEASE_PYTHON="${2:?--python needs a version}"; shift 2 ;;
    --platform) DOCKER_PLATFORM="${2:?--platform needs a value}"; shift 2 ;;
    -h|--help) sed -n '2,22p' "$SCRIPT_PATH"; exit 0 ;;
    *) echo "未知参数: ${1}（用 --help 查看用法）" >&2; exit 2 ;;
  esac
done

cd "$REPO_ROOT"

if [ "$PLAN" -eq 1 ]; then
  print_plan
  exit 0
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "→ 工作树不干净：自检包含未提交改动（这是预期用法）。"
fi

if [ "$DISPATCH" -eq 1 ]; then
  command -v gh >/dev/null 2>&1 || {
    echo "需要 gh（https://cli.github.com/）。或打开" >&2
    echo "https://github.com/${CANONICAL_REPOSITORY_SLUG}/actions/workflows/release-preflight.yml" >&2
    exit 1
  }
  REF="$(git rev-parse --abbrev-ref HEAD)"
  SHA="$(git rev-parse HEAD)"
  if [ -n "$(git status --porcelain)" ]; then
    echo "未提交改动不会出现在 GitHub 矩阵里。先 commit + push，再 --dispatch。" >&2
    exit 1
  fi
  if ! git merge-base --is-ancestor HEAD "github/${REF}" 2>/dev/null \
    && ! git merge-base --is-ancestor HEAD "origin/${REF}" 2>/dev/null; then
    echo "当前 HEAD ${SHA:0:8} 可能尚未推到 GitHub。先 push 再 --dispatch。" >&2
  fi
  echo "→ Dispatch Release preflight on ${REF} (${SHA:0:8})"
  gh workflow run "Release preflight" --repo "$CANONICAL_REPOSITORY_SLUG" --ref "$REF"
  echo "  https://github.com/${CANONICAL_REPOSITORY_SLUG}/actions/workflows/release-preflight.yml"
  echo "  这是唯一能覆盖 Windows 的格。本机 Docker 只能近似 ubuntu-latest。"
  if [ "$WAIT" -eq 0 ]; then
    echo "  加上 --wait 会阻塞到 9 格结束（失败则非 0）。"
    exit 0
  fi
  echo "→ Waiting for the dispatched run on ${SHA:0:8}…"
  run_id=""
  deadline=$((SECONDS + 180))
  while [ "$SECONDS" -lt "$deadline" ]; do
    run_id="$(
      gh run list \
        --repo "$CANONICAL_REPOSITORY_SLUG" \
        --workflow release-preflight.yml \
        --commit "$SHA" \
        --limit 1 \
        --json databaseId \
        --jq '.[0].databaseId // empty' 2>/dev/null || true
    )"
    if [ -n "$run_id" ]; then
      break
    fi
    sleep 3
  done
  if [ -z "$run_id" ]; then
    echo "找不到刚触发的 run。打开上面的 URL 查看。" >&2
    exit 1
  fi
  gh run watch "$run_id" --repo "$CANONICAL_REPOSITORY_SLUG" --exit-status
  exit $?
fi

command -v uv >/dev/null 2>&1 || { echo "需要 uv（https://docs.astral.sh/uv/）。" >&2; exit 1; }

hot_tests() {
  awk 'NF && $1 !~ /^#/' "$HOT_LIST"
}

run_cell() {
  local label="$1"
  local round="$2"
  echo "→ ${label} (round ${round}/${REPEAT})"
  uv run --no-sync --project cli ruff check cli/src cli/tests
  if [ "$FULL" -eq 1 ]; then
    echo "  pytest -q -m 'not release_gate'  (same as release.yml compatibility)"
    uv run --no-sync --project cli pytest -q -m "not release_gate"
    uv run --no-sync --project cli pytest -q \
      cli/tests/eval/test_reactive_binding_release_gate.py \
      cli/tests/eval/test_objective_provider_quality_offline_corpus.py
  else
    echo "  hot suite (release-flake cells); --full for the whole compatibility job"
    # shellcheck disable=SC2046
    uv run --no-sync --project cli pytest -q $(hot_tests)
  fi
}

HOST_GATE=""
cleanup_host() {
  if [ -n "${HOST_GATE:-}" ]; then
    rm -rf "$HOST_GATE"
  fi
}
trap cleanup_host EXIT

run_host_cell() {
  local py="${RELEASE_PYTHON:-3.12}"
  HOST_GATE="$(mktemp -d "${TMPDIR:-/tmp}/omni-release-selfcheck-XXXXXX")"
  echo "→ Host cell CPython ${py} (isolated; does not touch cli/.venv)"
  UV_PROJECT_ENVIRONMENT="$HOST_GATE/venv" \
    uv sync --project cli --python "$py" --all-extras --locked 2>/dev/null \
    || UV_PROJECT_ENVIRONMENT="$HOST_GATE/venv" \
         uv sync --project cli --python "$py" --all-extras
  local i
  for i in $(seq 1 "$REPEAT"); do
    UV_PROJECT_ENVIRONMENT="$HOST_GATE/venv" run_cell "this OS × ${py}" "$i"
  done
}

run_linux_cell() {
  local py="${RELEASE_PYTHON:-3.11}"
  local image="ghcr.io/astral-sh/uv:python${py}-bookworm"
  command -v docker >/dev/null 2>&1 || {
    echo "需要 docker 才能复现 GitHub ubuntu-latest。" >&2
    echo "没有 docker 时用: $SCRIPT_PATH --dispatch" >&2
    exit 1
  }
  echo "→ Linux cell ${image} platform=${DOCKER_PLATFORM}"
  echo "  Matches release.yml ubuntu-latest × ${py} (SQLite PRAGMA included)."
  echo "  Not Windows. Apple Silicon uses qemu unless --platform linux/arm64."
  local suite
  if [ "$FULL" -eq 1 ]; then
    suite="uv run --no-sync --project cli pytest -q -m 'not release_gate'
uv run --no-sync --project cli pytest -q \
  cli/tests/eval/test_reactive_binding_release_gate.py \
  cli/tests/eval/test_objective_provider_quality_offline_corpus.py"
  else
    suite="uv run --no-sync --project cli pytest -q $(hot_tests | tr '\n' ' ')"
  fi
  local i
  for i in $(seq 1 "$REPEAT"); do
    echo "→ linux/amd64 round ${i}/${REPEAT}"
    docker run --rm \
      --platform "$DOCKER_PLATFORM" \
      -v "$REPO_ROOT:/src" \
      -w /src \
      -e UV_PROJECT_ENVIRONMENT=/tmp/omni-release-venv \
      -e PYTHONDONTWRITEBYTECODE=1 \
      "$image" \
      bash -lc "set -euo pipefail
uv sync --project cli --all-extras
uv run --no-sync --project cli ruff check cli/src cli/tests
${suite}"
  done
}

if [ "$LINUX" -eq 1 ]; then
  run_linux_cell
else
  run_host_cell
fi

echo "✓ Local self-check green."
if [ "$LINUX" -eq 0 ]; then
  echo "  Linux SQLite next: $SCRIPT_PATH --linux"
fi
if [ "$FULL" -eq 0 ]; then
  echo "  Full compatibility cell: $SCRIPT_PATH --full"
fi
echo "  Windows / 9-cell matrix: $SCRIPT_PATH --dispatch --wait  (needs a pushed SHA)"
echo "  Job map: $SCRIPT_PATH --plan"
