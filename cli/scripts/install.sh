#!/usr/bin/env bash
# OmniScientist installer for macOS/Linux.
#
# The default is always an isolated uv tool. Missing uv installations are
# bootstrapped with Astral's official installer. CONDA_PREFIX and VIRTUAL_ENV
# are intentionally ignored unless --method env is supplied explicitly.
#
# Source policy:
#   * source checkout (has cli/pyproject.toml + src/omni) -> local snapshot by default;
#   * standalone installer (piped `curl … | sh`, no source tree) -> installs the
#     published PyPI package by default;
#   * a tracking channel (--channel master) follows a moving branch tip and is
#     non-reproducible; --remote --ref <tag-or-commit> stays immutable/pinned.
#
# Install == update for a checkout: rerun this script to deploy its current
# tree. Stable remote users install from PyPI and run `omni update`.
#
# Usage:
#   scripts/install.sh                                  # isolated uv tool (recommended)
#   scripts/install.sh --channel master                 # explicit development channel
#   scripts/install.sh --channel pypi                   # explicit published package channel
#   scripts/install.sh --local                          # explicit checkout snapshot
#   scripts/install.sh --editable --local               # contributor install in uv tool
#   scripts/install.sh --method env                     # explicit active venv/conda env
#   scripts/install.sh --method env --force-conda-base  # advanced, unsafe override
#   scripts/install.sh --on-conflict migrate            # noninteractive duplicate migration
#   scripts/install.sh --extras ""                      # omit optional extras
#   scripts/install.sh --index-url pypi                 # use the official PyPI index
#   scripts/install.sh --pypi                           # published PyPI package
#   scripts/install.sh --remote --ref <tag-or-commit>   # immutable public git source
#   scripts/install.sh --from <git-url> --ref <ref>     # immutable alternate git source

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." 2>/dev/null && pwd || true)"
EXTRAS="mcp,vec,channels,web"
EDITABLE=0
METHOD="uv"
FORCE_CONDA_BASE=0
ON_CONFLICT="ask"
SOURCE_MODE="auto" # auto | local | pypi | git
CHANNEL="${OMNI_INSTALL_CHANNEL:-}" # ""(auto) | master | pypi — user tracking channel
TRACK_BRANCH=0     # 1 while installing a moving branch tip (non-reproducible)
DEFAULT_TRACK_BRANCH="master" # explicit development channel
REMOTE_REPO="${OMNI_GIT_REPOSITORY:-}"
REF=""
INSTALLED_METHOD=""
INSTALLED_OMNI=""
INSTALLED_PYTHON=""
ENV_PY_OVERRIDE=""
MIGRATE_AFTER=0
MIGRATION_CLEANUP_FAILED=0
UV_OWNER_TOOL_DIR=""
UV_OWNER_BIN_DIR=""
ALIYUN_PYPI_INDEX_URL="https://mirrors.aliyun.com/pypi/simple/"
OFFICIAL_PYPI_INDEX_URL="https://pypi.org/simple/"
PACKAGE_INDEX_URL="${OMNI_PYPI_INDEX_URL:-$OFFICIAL_PYPI_INDEX_URL}"
INSTALL_WAIT_SECONDS="${OMNI_INSTALL_WAIT_SECONDS:-30}"
PROBE_TIMEOUT_SECONDS="${OMNI_INSTALL_PROBE_TIMEOUT_SECONDS:-3}"
INSTALL_LOCK_DIR=""
INSTALL_LOCK_HELD=0

case "$INSTALL_WAIT_SECONDS" in
  ''|*[!0-9]*|0) echo "OMNI_INSTALL_WAIT_SECONDS must be a positive integer." >&2; exit 2 ;;
esac
case "$PROBE_TIMEOUT_SECONDS" in
  ''|*[!0-9]*|0) echo "OMNI_INSTALL_PROBE_TIMEOUT_SECONDS must be a positive integer." >&2; exit 2 ;;
esac

while [ $# -gt 0 ]; do
  case "$1" in
    -e|--editable) EDITABLE=1; shift ;;
    --method) [ $# -ge 2 ] || { echo "--method requires uv or env." >&2; exit 2; }; METHOD="$2"; shift 2 ;;
    --force-conda-base) FORCE_CONDA_BASE=1; shift ;;
    --on-conflict) [ $# -ge 2 ] || { echo "--on-conflict requires ask, upgrade, migrate, or cancel." >&2; exit 2; }; ON_CONFLICT="$2"; shift 2 ;;
    --extras) EXTRAS="${2:-}"; shift 2 ;;
    --index-url) [ $# -ge 2 ] || { echo "--index-url requires aliyun, pypi, or an index URL." >&2; exit 2; }; PACKAGE_INDEX_URL="$2"; shift 2 ;;
    --local) SOURCE_MODE="local"; shift ;;
    --pypi) SOURCE_MODE="pypi"; shift ;;
    --remote) SOURCE_MODE="git"; shift ;;
    --channel) [ $# -ge 2 ] || { echo "--channel requires master or pypi." >&2; exit 2; }; CHANNEL="$2"; shift 2 ;;
    --from) [ $# -ge 2 ] || { echo "--from requires a git URL." >&2; exit 2; }; REMOTE_REPO="$2"; SOURCE_MODE="git"; shift 2 ;;
    --ref) [ $# -ge 2 ] || { echo "--ref requires a tag or commit." >&2; exit 2; }; REF="$2"; shift 2 ;;
    -h|--help) sed -n '/^# Usage:/,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1 (use --help)." >&2; exit 2 ;;
  esac
done

case "$(printf '%s' "$PACKAGE_INDEX_URL" | tr '[:upper:]' '[:lower:]')" in
  aliyun) PACKAGE_INDEX_URL="$ALIYUN_PYPI_INDEX_URL" ;;
  pypi|official) PACKAGE_INDEX_URL="$OFFICIAL_PYPI_INDEX_URL" ;;
esac
case "$PACKAGE_INDEX_URL" in
  http://*|https://*|file://*) ;;
  *) echo "Invalid --index-url: expected aliyun, pypi, or an http(s)/file URL." >&2; exit 2 ;;
esac

case "$METHOD" in
  uv|env) ;;
  auto)
    echo "Warning: --method auto is deprecated and now means --method uv." >&2
    METHOD="uv"
    ;;
  *) echo "Unknown --method: $METHOD (choose uv or env)." >&2; exit 2 ;;
esac

echo "-> Python package index: $PACKAGE_INDEX_URL"
case "$ON_CONFLICT" in
  ask|upgrade|migrate|cancel) ;;
  *) echo "Unknown --on-conflict: $ON_CONFLICT (choose ask, upgrade, migrate, or cancel)." >&2; exit 2 ;;
esac
if [ "$FORCE_CONDA_BASE" -eq 1 ] && [ "$METHOD" != "env" ]; then
  echo "--force-conda-base is only valid with --method env." >&2
  exit 2
fi

# A tracking "channel" is a convenience over the raw source modes: it selects
# where a *user* install pulls from and how `omni update` advances. "master"
# tracks the moving branch tip (always newest, non-reproducible); "pypi" is the
# published package. An explicit source flag (--local/--pypi/--remote/--from)
# always wins over --channel.
if [ -n "$CHANNEL" ]; then
  case "$CHANNEL" in
    master|pypi) ;;
    *) echo "Unknown --channel: $CHANNEL (choose master or pypi)." >&2; exit 2 ;;
  esac
  if [ "$SOURCE_MODE" = "auto" ]; then
    case "$CHANNEL" in
      master) SOURCE_MODE="git"; TRACK_BRANCH=1; [ -n "$REF" ] || REF="$DEFAULT_TRACK_BRANCH" ;;
      pypi) SOURCE_MODE="pypi" ;;
    esac
  fi
fi

# Running from a source checkout means the source is right here, so install
# from it regardless of the version string — a release-versioned working tree
# (e.g. 2.0.0, not 2.0.0.dev*) still installs locally instead of chasing a
# PyPI package that may not be published. A standalone installer copy with no
# source tree installs the published package. The moving master channel remains
# an explicit development choice via --channel master.
if [ "$SOURCE_MODE" = "auto" ]; then
  if [ -n "$REPO_DIR" ] && [ -f "$REPO_DIR/pyproject.toml" ] \
    && [ -f "$REPO_DIR/src/omni/__init__.py" ]; then
    SOURCE_MODE="local"
  else
    SOURCE_MODE="pypi"; CHANNEL="${CHANNEL:-pypi}"
  fi
fi

case "$SOURCE_MODE" in
  local)
    [ -n "$REPO_DIR" ] && [ -f "$REPO_DIR/pyproject.toml" ] || {
      echo "--local requires an OmniScientist source checkout." >&2; exit 2;
    }
    SPEC="$REPO_DIR"
    [ -n "$EXTRAS" ] && SPEC="$REPO_DIR[$EXTRAS]"
    echo "-> Source: local checkout snapshot ($REPO_DIR)"
    ;;
  pypi)
    [ "$EDITABLE" -eq 0 ] || { echo "--editable requires --local." >&2; exit 2; }
    SPEC="OmniScientist-V2"
    [ -n "$EXTRAS" ] && SPEC="OmniScientist-V2[$EXTRAS]"
    echo "-> Source: published PyPI package"
    ;;
  git)
    [ "$EDITABLE" -eq 0 ] || { echo "Remote installs do not support --editable; use --local." >&2; exit 2; }
    [ -n "$REF" ] || {
      echo "Remote installation requires --ref <immutable-tag-or-commit> (or --channel master)." >&2; exit 2;
    }
    if [ "$TRACK_BRANCH" -eq 1 ]; then
      echo "-> Tracking channel '$REF' (moving branch tip; non-reproducible)." >&2
      echo "   Pin with --remote --ref <tag-or-commit> for a reproducible install." >&2
    elif ! printf '%s\n' "$REF" | grep -Eq '^([0-9a-fA-F]{40}|v?[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.]+)?)$'; then
      echo "Ref '$REF' is not an immutable release tag or full 40-character commit hash (use --channel master to track a branch)." >&2
      exit 2
    fi
    [ -n "$REMOTE_REPO" ] || {
      echo "Remote Git installation requires --from <official-github-repository-url>." >&2; exit 2;
    }
    GIT_SPEC="git+${REMOTE_REPO}@${REF}#subdirectory=cli"
    if [ -n "$EXTRAS" ]; then SPEC="OmniScientist-V2[$EXTRAS] @ $GIT_SPEC"; else SPEC="OmniScientist-V2 @ $GIT_SPEC"; fi
    if [ "$TRACK_BRANCH" -eq 1 ]; then
      echo "-> Source: git channel ($GIT_SPEC)"
    else
      echo "-> Source: immutable git ref ($GIT_SPEC)"
    fi
    ;;
  *) echo "Internal error: unsupported source mode $SOURCE_MODE." >&2; exit 2 ;;
esac

install_state_dir() {
  if [ -n "${OMNI_INSTALL_STATE_DIR:-}" ]; then
    printf '%s\n' "$OMNI_INSTALL_STATE_DIR"
  elif [ -n "${XDG_STATE_HOME:-}" ]; then
    printf '%s/omni\n' "$XDG_STATE_HOME"
  else
    printf '%s/.local/state/omni\n' "${HOME:?HOME is required}"
  fi
}

wait_previous_uninstall() {
  local state_dir pending failed ticks=0 limit
  state_dir="$(install_state_dir)"
  pending="$state_dir/uninstall.pending"
  failed="$state_dir/uninstall.failed"
  limit=$((INSTALL_WAIT_SECONDS * 10))
  if [ -f "$pending" ]; then
    echo "-> Waiting for the previous Omni uninstall to finish"
    while [ -f "$pending" ] && [ "$ticks" -lt "$limit" ]; do
      sleep 0.1
      ticks=$((ticks + 1))
    done
    if [ -f "$pending" ]; then
      echo "Previous Omni uninstall did not finish within ${INSTALL_WAIT_SECONDS}s." >&2
      echo "Inspect or remove the stale operation marker after confirming no uninstall is running: $pending" >&2
      return 1
    fi
    echo "-> Previous Omni uninstall finished"
  fi
  if [ -f "$failed" ]; then
    echo "Warning: the previous program removal reported an error; reinstall will repair the uv tool." >&2
    echo "Failure record: $failed" >&2
  fi
}

release_install_lock() {
  [ "$INSTALL_LOCK_HELD" -eq 1 ] || return 0
  rm -f "$INSTALL_LOCK_DIR/pid"
  rmdir "$INSTALL_LOCK_DIR" 2>/dev/null || true
  INSTALL_LOCK_HELD=0
}

acquire_install_lock() {
  local state_dir owner=""
  state_dir="$(install_state_dir)"
  mkdir -p "$state_dir"
  INSTALL_LOCK_DIR="$state_dir/installer.lock"
  if ! mkdir "$INSTALL_LOCK_DIR" 2>/dev/null; then
    [ -f "$INSTALL_LOCK_DIR/pid" ] && owner="$(head -n 1 "$INSTALL_LOCK_DIR/pid" 2>/dev/null || true)"
    case "$owner" in
      ''|*[!0-9]*) ;;
      *)
        if kill -0 "$owner" 2>/dev/null; then
          echo "Another Omni installation is already running (pid $owner)." >&2
          return 1
        fi
        ;;
    esac
    rm -f "$INSTALL_LOCK_DIR/pid"
    rmdir "$INSTALL_LOCK_DIR" 2>/dev/null || {
      echo "Could not recover stale installer lock: $INSTALL_LOCK_DIR" >&2
      return 1
    }
    mkdir "$INSTALL_LOCK_DIR"
  fi
  printf '%s\n' "$$" > "$INSTALL_LOCK_DIR/pid"
  INSTALL_LOCK_HELD=1
  trap release_install_lock EXIT
}

active_python() {
  if [ -n "${VIRTUAL_ENV:-}" ] && [ -x "$VIRTUAL_ENV/bin/python" ]; then
    echo "$VIRTUAL_ENV/bin/python"; return 0
  fi
  if [ -n "${CONDA_PREFIX:-}" ] && [ -x "$CONDA_PREFIX/bin/python" ]; then
    echo "$CONDA_PREFIX/bin/python"; return 0
  fi
  return 1
}

entrypoint_python() {
  local path="$1" first=""
  first="$(head -n 1 "$path" 2>/dev/null || true)"
  case "$first" in '#!'*) printf '%s\n' "${first#\#!}" ;; *) printf '\n' ;; esac
}

method_for_python() {
  local normalized prefix
  [ -n "$1" ] || { printf 'env\n'; return 0; }
  prefix="$(dirname "$(dirname "$1")")"
  if [ -f "$prefix/uv-receipt.toml" ]; then
    printf 'uv\n'; return 0
  fi
  if [ -f "$prefix/pipx_metadata.json" ]; then
    printf 'pipx\n'; return 0
  fi
  normalized="$(printf '%s' "$1" | tr '\\' '/' | tr '[:upper:]' '[:lower:]')"
  case "$normalized" in
    *"/uv/tools/omniscientist-v2/"*|*"/uv/tools/omniscientist/"*) printf 'uv\n' ;;
    *"/pipx/venvs/omniscientist-v2/"*|*"/pipx/venvs/omniscientist/"*) printf 'pipx\n' ;;
    *) printf 'env\n' ;;
  esac
}

EXISTING_PATHS=()
EXISTING_METHODS=()
EXISTING_PYTHONS=()
EXISTING_VERSIONS=()

probe_omni_version() {
  local candidate="$1" output status pid ticks=0 limit version
  output="$(mktemp "${TMPDIR:-/tmp}/omni-version.XXXXXX")"
  status="${output}.status"
  limit=$((PROBE_TIMEOUT_SECONDS * 10))
  (
    set +e
    "$candidate" --version >"$output" 2>/dev/null
    printf '%s\n' "$?" >"$status"
  ) &
  pid=$!
  while [ ! -f "$status" ] && [ "$ticks" -lt "$limit" ]; do
    sleep 0.1
    ticks=$((ticks + 1))
  done
  if [ ! -f "$status" ]; then
    # Killing only the wrapper subshell leaves descendants (for example a hung
    # launcher's ``sleep``) alive. They can retain inherited descriptors and
    # make the caller wait until the original command eventually exits. Walk
    # the process tree leaf-first so the timeout is actually bounded on both
    # macOS and Linux, without depending on GNU ``timeout``/``pkill``.
    kill_process_tree "$pid" TERM
    sleep 0.1
    kill_process_tree "$pid" KILL
    wait "$pid" 2>/dev/null || true
    rm -f "$output" "$status"
    echo "Warning: timed out checking old Omni launcher after ${PROBE_TIMEOUT_SECONDS}s: $candidate" >&2
    return 124
  fi
  wait "$pid" 2>/dev/null || true
  version="$(head -n 1 "$output" 2>/dev/null || true)"
  rm -f "$output" "$status"
  printf '%s\n' "$version"
}

kill_process_tree() {
  local root="$1" signal="${2:-TERM}" child
  for child in $(ps -o pid= -P "$root" 2>/dev/null); do
    kill_process_tree "$child" "$signal"
  done
  kill "-$signal" "$root" 2>/dev/null || true
}

discover_installations() {
  local old_ifs="$IFS" dir candidate uv_bin manifest py_cmd seen=":"
  IFS=':'
  for dir in ${PATH:-}; do
    [ -n "$dir" ] || continue
    candidate="$dir/omni"
    consider_installation "$candidate" 1 "$seen"
    case "$seen" in *":$candidate:"*) ;; *) [ -f "$candidate" ] && seen="${seen}${candidate}:" ;; esac
  done
  IFS="$old_ifs"

  # uv's bin directory may not have reached PATH in this shell yet.
  if command -v uv >/dev/null 2>&1; then
    uv_bin="$(uv tool dir --bin 2>/dev/null || true)"
    candidate="$uv_bin/omni"
    case "$seen" in *":$candidate:"*) ;; *)
      consider_installation "$candidate" 0 "$seen"
      [ -f "$candidate" ] && seen="${seen}${candidate}:"
      ;;
    esac
  fi

  # Ownership records can reveal an installation that is currently shadowed.
  # Only execute a manifest launcher after validating it as an Omni entrypoint.
  manifest="${OMNI_HOME:-${HOME:-}/.omni}/install.json"
  py_cmd="$(command -v python3 || command -v python || true)"
  if [ -n "$py_cmd" ] && [ -f "$manifest" ]; then
    while IFS= read -r candidate; do
      [ -n "$candidate" ] || continue
      case "$seen" in *":$candidate:"*) continue ;; esac
      consider_installation "$candidate" 0 "$seen"
      [ -f "$candidate" ] && seen="${seen}${candidate}:"
    done < <("$py_cmd" -c 'import json,sys; data=json.load(open(sys.argv[1], encoding="utf-8")); print("\n".join(str(row.get("executable", "")) for row in data.get("installations", []) if isinstance(row, dict)))' "$manifest" 2>/dev/null || true)
  fi
  return 0
}

consider_installation() {
  local candidate="$1" path_trusted="$2" _seen="$3" version py method i
  [ -f "$candidate" ] && [ -x "$candidate" ] || return 0
  case "$_seen" in *":$candidate:"*) return 0 ;; esac
  if [ "$path_trusted" -ne 1 ] && ! head -c 8192 "$candidate" 2>/dev/null | grep -aq 'omni\.cli\.main'; then
    return 0
  fi
  version="$(probe_omni_version "$candidate" || true)"
  case "$version" in *OmniScientist*) ;; *) return 0 ;; esac
  py="$(entrypoint_python "$candidate")"
  method="$(method_for_python "$py")"
  if [ -n "$py" ]; then
    for ((i = 0; i < ${#EXISTING_PATHS[@]}; i++)); do
      if [ "${EXISTING_METHODS[$i]}" = "$method" ] && [ "${EXISTING_PYTHONS[$i]}" = "$py" ]; then
        return 0
      fi
    done
  fi
  EXISTING_PATHS+=("$candidate")
  EXISTING_METHODS+=("$method")
  EXISTING_PYTHONS+=("$py")
  EXISTING_VERSIONS+=("$version")
}

resolve_duplicate_installations() {
  [ "${#EXISTING_PATHS[@]}" -gt 0 ] || return 0
  echo
  echo "Existing OmniScientist installation(s) detected:"
  local i action="$ON_CONFLICT"
  for ((i = 0; i < ${#EXISTING_PATHS[@]}; i++)); do
    printf '  %d) %s [%s; %s]\n' "$((i + 1))" "${EXISTING_PATHS[$i]}" "${EXISTING_METHODS[$i]}" "${EXISTING_VERSIONS[$i]}"
  done
  if [ "$action" = "ask" ] && [ "${#EXISTING_PATHS[@]}" -eq 1 ] \
    && [ "${EXISTING_METHODS[0]}" = "uv" ] && [ "$METHOD" = "uv" ]; then
    action="upgrade"
    echo "-> Existing uv installation will be upgraded in place"
  fi
  if [ "$action" = "ask" ]; then
    if [ -t 0 ]; then
      echo "Choose: [1] upgrade existing  [2] migrate to uv  [3] cancel"
      printf '> '
      read -r choice
      case "$choice" in 1|upgrade) action="upgrade" ;; 2|migrate) action="migrate" ;; *) action="cancel" ;; esac
    else
      echo "Installation stopped: choose upgrade, migrate, or cancel with --on-conflict." >&2
      echo "Example: $0 --on-conflict migrate" >&2
      return 2
    fi
  fi
  case "$action" in
    cancel)
      echo "Installation cancelled; no changes were made."
      exit 0
      ;;
    migrate)
      local j
      for ((j = 0; j < ${#EXISTING_PATHS[@]}; j++)); do
        if [ "${EXISTING_METHODS[$j]}" = "env" ] \
          && { [ -z "${EXISTING_PYTHONS[$j]}" ] || [ ! -x "${EXISTING_PYTHONS[$j]}" ]; }; then
          echo "Cannot safely migrate ${EXISTING_PATHS[$j]} because its owner cannot be bound." >&2
          echo "Remove that launcher manually or choose cancel; no installation changes were made." >&2
          return 2
        fi
      done
      METHOD="uv"
      MIGRATE_AFTER=1
      ;;
    upgrade)
      case "${EXISTING_METHODS[0]}" in
        uv)
          [ -n "${EXISTING_PYTHONS[0]}" ] && [ -x "${EXISTING_PYTHONS[0]}" ] || {
            echo "Cannot bind the existing uv registry for ${EXISTING_PATHS[0]}; choose migrate or cancel." >&2
            return 2
          }
          METHOD="uv"
          UV_OWNER_TOOL_DIR="$(dirname "$(dirname "$(dirname "${EXISTING_PYTHONS[0]}")")")"
          UV_OWNER_BIN_DIR="$(dirname "${EXISTING_PATHS[0]}")"
          ;;
        pipx)
          echo "The existing launcher is owned by pipx; the repository installer will not mutate its managed venv." >&2
          echo "Use 'pipx install --force <source>' directly, or rerun with --on-conflict migrate to consolidate into uv." >&2
          return 2
          ;;
        env)
          [ -n "${EXISTING_PYTHONS[0]}" ] && [ -x "${EXISTING_PYTHONS[0]}" ] || {
            echo "Cannot identify the Python owner of ${EXISTING_PATHS[0]}; choose migrate or cancel." >&2
            return 2
          }
          METHOD="env"
          ENV_PY_OVERRIDE="${EXISTING_PYTHONS[0]}"
          ;;
      esac
      if [ "${#EXISTING_PATHS[@]}" -gt 1 ]; then
        echo "Warning: upgrade targets the first PATH installation; other copies remain. Use migrate to consolidate." >&2
      fi
      ;;
  esac
}

is_conda_base_python() {
  local py="$1" base=""
  if [ -n "${CONDA_PREFIX:-}" ] && [ "$py" = "$CONDA_PREFIX/bin/python" ] \
    && [ "${CONDA_DEFAULT_ENV:-}" = "base" ]; then
    return 0
  fi
  if command -v conda >/dev/null 2>&1; then
    base="$(conda info --base 2>/dev/null || true)"
    [ -n "$base" ] && [ "$py" = "$base/bin/python" ] && return 0
  fi
  return 1
}

guard_env_target() {
  local py="$1"
  if is_conda_base_python "$py" && [ "$FORCE_CONDA_BASE" -ne 1 ]; then
    echo "Refusing to install OmniScientist into Conda base." >&2
    echo "Use the default isolated uv tool, activate a dedicated env, or explicitly add --force-conda-base." >&2
    return 2
  fi
}

prepare_local_web_ui() {
  [ "$SOURCE_MODE" = "local" ] || return 0
  local script="$REPO_DIR/scripts/build_web_ui.sh"
  local package_json="$REPO_DIR/../web/package.json"
  [ -f "$package_json" ] || return 0
  if [ ! -f "$script" ]; then
    echo "Local Web UI source exists, but its build script is missing: $script" >&2
    return 1
  fi
  echo "-> Building the loopback SPA from this checkout"
  bash "$script"
}

install_into_env() {
  local py="$1" scripts
  guard_env_target "$py"
  echo "-> Installing into explicitly selected Python env: $py"
  # Compile .pyc at install time (uv skips it by default) so the first launch
  # and the research-pptx setup step don't pay a cold bytecode-compile pause.
  export UV_COMPILE_BYTECODE=1
  if command -v uv >/dev/null 2>&1; then
    if [ "$EDITABLE" -eq 1 ]; then
      uv pip install --python "$py" --default-index "$PACKAGE_INDEX_URL" --editable "$SPEC"
    elif [ "$SOURCE_MODE" = "local" ]; then
      uv pip install --python "$py" --default-index "$PACKAGE_INDEX_URL" --reinstall-package OmniScientist-V2 "$SPEC"
    elif [ "$TRACK_BRANCH" -eq 1 ]; then
      # Re-resolve the moving branch tip and force a reinstall so a rerun updates.
      uv pip install --python "$py" --refresh --reinstall-package OmniScientist-V2 --default-index "$PACKAGE_INDEX_URL" "$SPEC"
    else
      uv pip install --python "$py" --default-index "$PACKAGE_INDEX_URL" "$SPEC"
    fi
  else
    if [ "$EDITABLE" -eq 1 ]; then
      "$py" -m pip install --index-url "$PACKAGE_INDEX_URL" --editable "$SPEC"
    elif [ "$SOURCE_MODE" = "local" ] || [ "$TRACK_BRANCH" -eq 1 ]; then
      "$py" -m pip install --index-url "$PACKAGE_INDEX_URL" --force-reinstall "$SPEC"
    else
      "$py" -m pip install --index-url "$PACKAGE_INDEX_URL" "$SPEC"
    fi
  fi
  scripts="$("$py" -c 'import sysconfig; print(sysconfig.get_path("scripts"))')"
  INSTALLED_METHOD="env"
  INSTALLED_PYTHON="$py"
  INSTALLED_OMNI="$scripts/omni"
}

run_owned_uv() {
  local uv_cmd="$1"
  shift
  if [ -n "$UV_OWNER_TOOL_DIR" ]; then
    UV_TOOL_DIR="$UV_OWNER_TOOL_DIR" UV_TOOL_BIN_DIR="$UV_OWNER_BIN_DIR" \
      "$uv_cmd" "$@"
  else
    "$uv_cmd" "$@"
  fi
}

install_uv_tool() {
  local uv_cmd uv_bin
  uv_cmd="$(find_or_install_uv)"
  echo "-> Installing with uv tool into an isolated environment"
  if [ "$EDITABLE" -eq 1 ]; then
    run_owned_uv "$uv_cmd" tool install --force --default-index "$PACKAGE_INDEX_URL" --editable "$SPEC"
  elif [ "$SOURCE_MODE" = "local" ]; then
    run_owned_uv "$uv_cmd" tool install --force --default-index "$PACKAGE_INDEX_URL" --reinstall-package OmniScientist-V2 "$SPEC"
  elif [ "$TRACK_BRANCH" -eq 1 ]; then
    # --refresh defeats uv's git cache so the moving branch tip is re-resolved,
    # making a rerun of the installer a real "update to latest master".
    run_owned_uv "$uv_cmd" tool install --force --refresh --default-index "$PACKAGE_INDEX_URL" "$SPEC"
  else
    run_owned_uv "$uv_cmd" tool install --force --default-index "$PACKAGE_INDEX_URL" "$SPEC"
  fi
  run_owned_uv "$uv_cmd" tool update-shell || true
  uv_bin="$(run_owned_uv "$uv_cmd" tool dir --bin)"
  INSTALLED_METHOD="uv"
  INSTALLED_OMNI="$uv_bin/omni"
  INSTALLED_PYTHON="$(entrypoint_python "$INSTALLED_OMNI")"
  echo "(uv bin: $uv_bin; reopen the terminal if PATH has not refreshed.)"
}

find_or_install_uv() {
  local uv_cmd candidate installer_url="https://astral.sh/uv/install.sh"
  uv_cmd="$(command -v uv || true)"
  if [ -n "$uv_cmd" ]; then
    printf '%s\n' "$uv_cmd"
    return 0
  fi

  echo "-> uv not found; installing it with Astral's official installer" >&2
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf "$installer_url" | sh 1>&2
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- "$installer_url" | sh 1>&2
  else
    echo "Cannot install uv automatically: curl or wget is required." >&2
    return 1
  fi

  uv_cmd="$(command -v uv || true)"
  if [ -n "$uv_cmd" ]; then
    printf '%s\n' "$uv_cmd"
    return 0
  fi
  for candidate in \
    "${UV_INSTALL_DIR:+$UV_INSTALL_DIR/uv}" \
    "${HOME:-}/.local/bin/uv" \
    "${HOME:-}/.cargo/bin/uv"; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  echo "uv installation completed, but its executable could not be located." >&2
  echo "Open a new terminal or install uv manually: https://docs.astral.sh/uv/" >&2
  return 1
}

remove_previous_installations() {
  [ "$MIGRATE_AFTER" -eq 1 ] || return 0
  local i old_path old_method old_python old_prefix owner_home owner_bin cleanup_bin temporary_bin=""
  for ((i = 0; i < ${#EXISTING_PATHS[@]}; i++)); do
    old_path="${EXISTING_PATHS[$i]}"
    old_method="${EXISTING_METHODS[$i]}"
    old_python="${EXISTING_PYTHONS[$i]}"
    if [ -n "$old_python" ] && [ "$old_python" = "$INSTALLED_PYTHON" ]; then
      continue
    fi
    if [ "$old_method" = "env" ] && [ "$old_path" = "$INSTALLED_OMNI" ]; then
      continue
    fi
    if [ -n "$old_python" ] && [ -x "$old_python" ]; then
      echo "-> Removing previous $old_method installation owned by $old_python"
      if [ "$old_method" = "pipx" ]; then
        if command -v pipx >/dev/null 2>&1; then
          # <PIPX_HOME>/venvs/omniscientist-v2/bin/python. Bind both registry
          # roots so uninstall removes the matching metadata and launcher,
          # never a different default pipx installation.
          owner_home="$(dirname "$(dirname "$(dirname "$(dirname "$old_python")")")")"
          owner_bin="$(dirname "$old_path")"
          cleanup_bin="$owner_bin"
          temporary_bin=""
          if [ "$old_path" = "$INSTALLED_OMNI" ]; then
            # The new uv launcher replaced the pipx shim at the same path.
            # Remove pipx's venv/metadata without unlinking the new launcher.
            temporary_bin="$(mktemp -d "${TMPDIR:-/tmp}/omni-pipx-cleanup.XXXXXX")"
            cleanup_bin="$temporary_bin"
          fi
          PIPX_HOME="$owner_home" \
            PIPX_BIN_DIR="$cleanup_bin" \
            PIPX_MAN_DIR="$owner_home/man" \
            pipx uninstall OmniScientist-V2 || {
              echo "Warning: could not remove $old_path" >&2
              MIGRATION_CLEANUP_FAILED=1
            }
          [ -z "$temporary_bin" ] || rmdir "$temporary_bin" 2>/dev/null || true
        else
          echo "Warning: pipx owns $old_path but is not on PATH; it was left in place." >&2
          MIGRATION_CLEANUP_FAILED=1
        fi
      elif [ "$old_method" = "uv" ]; then
        if command -v uv >/dev/null 2>&1; then
          old_prefix="$(dirname "$(dirname "$old_python")")"
          owner_home="$(dirname "$old_prefix")"
          owner_bin="$(dirname "$old_path")"
          cleanup_bin="$owner_bin"
          temporary_bin=""
          if [ "$old_path" = "$INSTALLED_OMNI" ]; then
            temporary_bin="$(mktemp -d "${TMPDIR:-/tmp}/omni-uv-cleanup.XXXXXX")"
            cleanup_bin="$temporary_bin"
          fi
          UV_TOOL_DIR="$owner_home" UV_TOOL_BIN_DIR="$cleanup_bin" \
            uv tool uninstall OmniScientist-V2 || {
              echo "Warning: could not remove $old_path" >&2
              MIGRATION_CLEANUP_FAILED=1
            }
          [ -z "$temporary_bin" ] || rmdir "$temporary_bin" 2>/dev/null || true
        else
          echo "Warning: uv owns $old_path but is not on PATH; it was left in place." >&2
          MIGRATION_CLEANUP_FAILED=1
        fi
      elif command -v uv >/dev/null 2>&1; then
        uv pip uninstall --python "$old_python" OmniScientist-V2 || {
          echo "Warning: could not remove $old_path" >&2
          MIGRATION_CLEANUP_FAILED=1
        }
      else
        "$old_python" -m pip uninstall -y OmniScientist-V2 || {
          echo "Warning: could not remove $old_path" >&2
          MIGRATION_CLEANUP_FAILED=1
        }
      fi
    else
      echo "Warning: could not identify the owner of $old_path; it was left in place." >&2
      MIGRATION_CLEANUP_FAILED=1
    fi
  done
  if [ "$MIGRATION_CLEANUP_FAILED" -ne 0 ]; then
    echo "Migration is incomplete: at least one previous Omni installation remains." >&2
    echo "Resolve the warnings above, then rerun the installer; ownership metadata was not changed." >&2
    return 1
  fi
}

wait_previous_uninstall
acquire_install_lock
echo "-> Checking existing Omni installations"
discover_installations
resolve_duplicate_installations
prepare_local_web_ui

case "$METHOD" in
  uv) install_uv_tool ;;
  env)
    PY="$ENV_PY_OVERRIDE"
    [ -n "$PY" ] || PY="$(active_python || true)"
    [ -n "$PY" ] || {
      echo "--method env requires an explicitly active venv/conda environment." >&2
      exit 1
    }
    install_into_env "$PY"
    ;;
esac

echo
if [ ! -x "$INSTALLED_OMNI" ]; then
  echo "Installation command completed, but the expected launcher was not found: $INSTALLED_OMNI" >&2
  exit 1
fi

echo "OK installed: $INSTALLED_OMNI"
"$INSTALLED_OMNI" --version
remove_previous_installations
# Record the update "channel" so `omni update` can read intent explicitly
# (master = branch tip, pypi = published, local/editable = developer checkout,
# pinned = immutable git ref). This is advisory metadata; `omni update` still
# falls back to the recorded source spec / direct_url.json if it is absent.
RECORD_CHANNEL="$CHANNEL"
if [ -z "$RECORD_CHANNEL" ]; then
  case "$SOURCE_MODE" in
    pypi) RECORD_CHANNEL="pypi" ;;
    git) [ "$TRACK_BRANCH" -eq 1 ] && RECORD_CHANNEL="$REF" || RECORD_CHANNEL="pinned" ;;
    local) [ "$EDITABLE" -eq 1 ] && RECORD_CHANNEL="editable" || RECORD_CHANNEL="local" ;;
  esac
fi
RECORD_ARGS=("$INSTALLED_OMNI" _record-install --method "$INSTALLED_METHOD" --source "$SPEC")
[ "$EDITABLE" -eq 1 ] && RECORD_ARGS+=(--editable)
[ -n "$RECORD_CHANNEL" ] && RECORD_ARGS+=(--channel "$RECORD_CHANNEL")
"${RECORD_ARGS[@]}" || echo "Warning: installation ownership metadata could not be recorded." >&2
rm -f "$(install_state_dir)/uninstall.failed"

echo "-> Converging bundled runtimes and Home Service"
"$INSTALLED_OMNI" _converge-install

echo "First-time setup: omni init"
echo 'Or run: omni   (the first bare `omni` launch opens the same setup wizard automatically)'
if [ "$TRACK_BRANCH" -eq 1 ]; then
  echo "Update (latest master): omni update   # re-resolves the branch tip each run"
elif [ "$SOURCE_MODE" = "local" ]; then
  echo "Redeploy this checkout (incl. uncommitted): ./cli/scripts/install.sh"
  [ "$EDITABLE" -eq 1 ] && echo "  editable install: pure-Python edits are live on next launch; rerun the installer to re-sync dependencies"
  echo "Pull + reinstall from git: omni update"
else
  echo "Later updates: omni update"
fi
echo "Installation diagnostics: omni doctor"
echo "Uninstall preview: omni uninstall --dry-run"
