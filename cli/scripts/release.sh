#!/usr/bin/env bash
# OmniScientist release — validate, create an immutable tag, and push it.
#
# The version is single-sourced from ``src/omni/__init__.py`` (hatch reads it),
# so there is nothing to bump in pyproject.toml. Bump ``__version__`` first,
# commit, then run this script.
#
# Usage:
#   scripts/release.sh                 # build + tag + push vX.Y.Z (with confirm)
#   scripts/release.sh --preflight     # same local cell as GitHub release.yml
#                                      # (ruff + pytest + gates + wheel + smoke);
#                                      # no tag. Does not replace the Windows
#                                      # matrix — run Actions → Release preflight
#                                      # for that.
#   scripts/release.sh --dry-run       # wheel + smoke only; no tests, no tag
#   scripts/release.sh --yes           # skip the confirmation prompt
#
# Default local preflight is ruff + wheel + isolated smoke. --preflight (or
# OMNI_RELEASE_LOCAL_TESTS=1) adds the GitHub compatibility cell on this OS:
# pytest -q -m "not release_gate" plus the two release-gate files the build
# job runs. Isolated temp venv — does not touch cli/.venv. Do not use
# ``uv run --python`` here; it can recreate that environment mid-suite.
# Expect ~45+ minutes. OMNI_RELEASE_PYTHON defaults to 3.12 (same as the
# Actions build job).
#
# Before tagging, run --preflight locally and Actions → Release preflight
# (or wait for CI green, including Windows). The tag still re-runs that
# matrix in release.yml and is the publish gate.
#
# The pushed GitHub tag triggers .github/workflows/release.yml. That workflow
# rebuilds, runs three-platform tests + wheel smoke, and publishes to PyPI
# through Trusted Publisher OIDC. Requires `uv` and `git`.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_PATH="$SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")"
CLI_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$CLI_DIR"

DRY_RUN=0
PREFLIGHT=0
ASSUME_YES=0
CANONICAL_REPOSITORY_SLUG="tsinghua-fib-lab/OmniScientist-V2"
CANONICAL_REPOSITORY_URL="https://github.com/$CANONICAL_REPOSITORY_SLUG"
CANONICAL_RELEASE_BRANCH="master"

is_canonical_repository_url() {
  # Accept https, git@github.com, and SSH host aliases such as
  # git@github.com-<profile>:tsinghua-fib-lab/OmniScientist-V2.git.
  case "$1" in
    "$CANONICAL_REPOSITORY_URL"|"$CANONICAL_REPOSITORY_URL.git" \
      |"https://github.com/$CANONICAL_REPOSITORY_SLUG" \
      |"https://github.com/$CANONICAL_REPOSITORY_SLUG.git" \
      |"git@github.com:$CANONICAL_REPOSITORY_SLUG" \
      |"git@github.com:$CANONICAL_REPOSITORY_SLUG.git" \
      |"git@"*":$CANONICAL_REPOSITORY_SLUG" \
      |"git@"*":$CANONICAL_REPOSITORY_SLUG.git" \
      |"ssh://git@github.com/$CANONICAL_REPOSITORY_SLUG" \
      |"ssh://git@github.com/$CANONICAL_REPOSITORY_SLUG.git" \
      |"ssh://git@"*"/$CANONICAL_REPOSITORY_SLUG" \
      |"ssh://git@"*"/$CANONICAL_REPOSITORY_SLUG.git") return 0 ;;
    *) return 1 ;;
  esac
}

require_canonical_repository_urls() {
  local kind="$1"
  local urls="$2"
  local url
  if [ -z "$urls" ]; then
    echo "origin $kind URL 缺失；正式发布只允许使用 ${CANONICAL_REPOSITORY_URL}。" >&2
    return 1
  fi
  while IFS= read -r url; do
    if ! is_canonical_repository_url "$url"; then
      echo "正式发布只允许使用 GitHub 权威仓库 ${CANONICAL_REPOSITORY_URL}。" >&2
      echo "检测到非权威 origin $kind URL: ${url}。" >&2
      return 1
    fi
  done <<< "$urls"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --preflight) PREFLIGHT=1; DRY_RUN=1; shift ;;
    -y|--yes) ASSUME_YES=1; shift ;;
    -h|--help) sed -n '2,32p' "$SCRIPT_PATH"; exit 0 ;;
    *) echo "未知参数: ${1}（用 --help 查看用法）" >&2; exit 2 ;;
  esac
done

command -v uv >/dev/null 2>&1 || { echo "需要 uv（https://docs.astral.sh/uv/）。" >&2; exit 1; }
command -v git >/dev/null 2>&1 || { echo "需要 git。" >&2; exit 1; }

for legal_file in LICENSE NOTICE; do
  if ! cmp -s "../$legal_file" "$legal_file"; then
    echo "$legal_file 与仓库根目录版本不一致；请先同步再发布。" >&2
    exit 1
  fi
done

if [ -n "$(git status --porcelain)" ]; then
  echo "工作树不干净；请先提交或清理改动后再发布。" >&2
  exit 1
fi

VERSION="$(sed -n 's/^__version__ *= *["'\'']\([^"'\'']*\)["'\''].*/\1/p' src/omni/__init__.py | head -n1)"
[ -n "$VERSION" ] || { echo "无法从 src/omni/__init__.py 解析 __version__。" >&2; exit 1; }
TAG="v$VERSION"
# Use ${VERSION} and ASCII parens: Bash 5.3 treats `${VERSION}（` as one name.
echo "-> Release OmniScientist ${VERSION} (tag ${TAG})"

# Development/local versions are buildable with --dry-run but never taggable.
# Public releases are an immutable X.Y.Z or intentional X.Y.ZrcN.
if ! [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+(rc[0-9]+)?$ ]] \
  && [ "$DRY_RUN" -eq 0 ]; then
  echo "当前版本 $VERSION 不是可发布的 stable/rc 版本（要求 X.Y.Z 或 X.Y.ZrcN）。" >&2
  exit 1
fi

# Guard: refuse to reuse an existing tag (prevents accidental re-release).
if [ "$DRY_RUN" -eq 0 ] && git rev-parse "$TAG" >/dev/null 2>&1; then
  echo "git tag $TAG 已存在。请先 bump __version__ 再发布。" >&2
  exit 1
fi

if [ "$DRY_RUN" -eq 0 ]; then
  ORIGIN_FETCH_URLS="$(git remote get-url --all origin 2>/dev/null || true)"
  ORIGIN_PUSH_URLS="$(git remote get-url --push --all origin 2>/dev/null || true)"
  require_canonical_repository_urls "fetch" "$ORIGIN_FETCH_URLS" || exit 1
  require_canonical_repository_urls "push" "$ORIGIN_PUSH_URLS" || exit 1
  if ! grep -Fqx "Repository = \"$CANONICAL_REPOSITORY_URL\"" pyproject.toml; then
    echo "cli/pyproject.toml 的 Repository 必须是 ${CANONICAL_REPOSITORY_URL}。" >&2
    echo "发布脚本不会发布带错误来源元数据的包。" >&2
    exit 1
  fi
  CURRENT_BRANCH="$(git branch --show-current)"
  if [ "$CURRENT_BRANCH" != "$CANONICAL_RELEASE_BRANCH" ]; then
    echo "正式发布必须从 $CANONICAL_RELEASE_BRANCH 分支执行；当前分支: ${CURRENT_BRANCH:-<detached>}。" >&2
    exit 1
  fi
  LOCAL_HEAD="$(git rev-parse HEAD)"
  if ! REMOTE_BRANCH="$(git ls-remote --exit-code origin "refs/heads/$CANONICAL_RELEASE_BRANCH")"; then
    echo "GitHub 尚无可验证的 $CANONICAL_RELEASE_BRANCH 分支。" >&2
    echo "首次发布前请执行 git push -u origin ${CANONICAL_RELEASE_BRANCH}。" >&2
    exit 1
  fi
  REMOTE_HEAD="${REMOTE_BRANCH%%[[:space:]]*}"
  if [ "$REMOTE_HEAD" != "$LOCAL_HEAD" ]; then
    echo "本地 HEAD 尚未同步到 origin/${CANONICAL_RELEASE_BRANCH}；拒绝创建仅含 tag 的发布。" >&2
    echo "请先执行 git push origin ${CANONICAL_RELEASE_BRANCH}。" >&2
    exit 1
  fi
fi

if [ "$ASSUME_YES" -eq 0 ] && [ "$DRY_RUN" -eq 0 ]; then
  printf "继续构建并发布 %s？[y/N] " "$VERSION"
  read -r ans
  case "$ans" in y|Y|yes|YES) ;; *) echo "已取消。"; exit 0 ;; esac
fi

RELEASE_PYTHON="${OMNI_RELEASE_PYTHON:-3.12}"
GATE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/omni-release-gate-XXXXXX")"
cleanup_release_dirs() {
  rm -rf "$GATE_DIR" "${SMOKE_DIR:-}"
}
trap cleanup_release_dirs EXIT

echo "→ Local preflight on CPython ${RELEASE_PYTHON} (isolated; does not touch cli/.venv)"
if [ "$PREFLIGHT" -eq 0 ] && [ "${OMNI_RELEASE_LOCAL_TESTS:-0}" != "1" ]; then
  echo "  This OS × Python cell: ruff + wheel + smoke. Full pytest: --preflight"
  echo "  Windows/macOS/Linux matrix still runs on GitHub Actions."
fi
UV_PROJECT_ENVIRONMENT="$GATE_DIR/venv" \
  uv sync --python "$RELEASE_PYTHON" --all-extras --locked 2>/dev/null \
  || UV_PROJECT_ENVIRONMENT="$GATE_DIR/venv" \
       uv sync --python "$RELEASE_PYTHON" --all-extras
UV_PROJECT_ENVIRONMENT="$GATE_DIR/venv" \
  uv run --no-sync ruff check src tests

if [ "$PREFLIGHT" -eq 1 ] || [ "${OMNI_RELEASE_LOCAL_TESTS:-0}" = "1" ]; then
  echo "→ GitHub-compatible test cell (this OS / CPython ${RELEASE_PYTHON} only)"
  echo "  Same commands as release.yml compatibility + the build-job gates."
  echo "  Does not exercise Windows runners — use Actions → Release preflight."
  UV_PROJECT_ENVIRONMENT="$GATE_DIR/venv" \
    uv run --no-sync pytest -q -m "not release_gate"
  UV_PROJECT_ENVIRONMENT="$GATE_DIR/venv" \
    uv run --no-sync pytest -q \
      tests/eval/test_reactive_binding_release_gate.py \
      tests/eval/test_objective_provider_quality_offline_corpus.py
fi

echo "→ Build wheel/sdist"
rm -rf dist
uv build --python "$RELEASE_PYTHON" --no-sources
python3 scripts/check_dist.py dist

echo "→ Isolated wheel smoke install"
SMOKE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/omni-release-smoke-XXXXXX")"
uv venv --python "$RELEASE_PYTHON" "$SMOKE_DIR/venv"
uv pip install --python "$SMOKE_DIR/venv/bin/python" dist/*.whl
"$SMOKE_DIR/venv/bin/omni" --version

if [ "$DRY_RUN" -eq 1 ]; then
  if [ "$PREFLIGHT" -eq 1 ]; then
    echo "✓ --preflight: local GitHub cell green; artifacts in dist/."
    echo "  Tag only after Actions → Release preflight (or CI) is green on Windows too."
  else
    echo "✓ --dry-run: build only; artifacts in dist/."
  fi
  ls -1 dist
  exit 0
fi

echo "→ Tag and push to GitHub: ${TAG}"
git tag -a "$TAG" -m "OmniScientist $VERSION"
git push origin "$TAG"

echo "✓ Pushed ${TAG}. GitHub Actions Release is now the publish gate (OIDC → PyPI)."
echo "  Next: Actions → Release → approve the pypi environment when publish waits."
echo "  After publish: uv tool install 'OmniScientist-V2==${VERSION}'"
