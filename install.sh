#!/bin/sh
# Install a persistent, per-user TodoGC command and its bundled agent skills.
# Requires a POSIX shell; never activates a project environment or invokes pip.
set -eu
set -f

fail() { printf '%s\n' "TodoGC installer: $*" >&2; exit 1; }
check_persistable_path() {
    # uv/pipx persist shell text. Reject characters that need shell-specific
    # escaping before handing them a directory; data/source arguments remain
    # unrestricted and are always passed as individual argv entries.
    case "$1" in
        *'$'*|*'`'*|*'"'*|*'\'*|*':'*|*'
'*) fail "Shell PATH persistence cannot safely represent this directory: $1. Choose a tool directory without shell expansion characters or colons." ;;
    esac
}
usage() {
    cat <<'HELP'
Usage: sh install.sh [--target codex|claude-code|all] [--source PACKAGE_OR_PATH]
                     [--db PATH] [--config PATH] [--offline]
Defaults: --target all --source todogc>=0.14.0. Existing uv/pipx installs are upgraded in place.
Use an absolute wheel/source path for local installs. --offline requires an existing manager.
HELP
}

target=all
source_spec='todogc>=0.14.0'
source_explicit=no
database=
config=
offline=no
while [ "$#" -gt 0 ]; do
    case "$1" in
        --target|--source|--db|--config)
            [ "$#" -ge 2 ] || fail "Missing value for $1"
            [ -n "$2" ] || fail "Empty value for $1"
            case "$1" in
                --target) target=$2 ;;
                --source) source_spec=$2; source_explicit=yes ;;
                --db) database=$2 ;;
                --config) config=$2 ;;
            esac
            shift 2 ;;
        --offline) offline=yes; shift ;;
        --help|-h) usage; exit 0 ;;
        *) fail "Unknown argument: $1" ;;
    esac
done
case "$target" in codex|claude-code|all) ;; *) fail "Invalid target: $target" ;; esac
case "$source_spec" in -*) fail "Source must be a package specification or path, not an option" ;; esac
[ -n "${HOME:-}" ] || fail "HOME is required"
check_persistable_path "$HOME"

# An activated agent/project environment must not choose the installed runtime.
# Drop empty PATH components too, so the working directory cannot supply a command.
clean_path=
old_ifs=$IFS
IFS=:
for entry in ${PATH:-}; do
    [ -n "$entry" ] || continue
    if [ -n "${VIRTUAL_ENV:-}" ] && [ "$entry" = "$VIRTUAL_ENV/bin" ]; then continue; fi
    if [ -n "${CONDA_PREFIX:-}" ] && [ "$entry" = "$CONDA_PREFIX/bin" ]; then continue; fi
    clean_path=${clean_path:+$clean_path:}$entry
done
IFS=$old_ifs
PATH=$clean_path
export PATH
unset VIRTUAL_ENV CONDA_PREFIX PYTHONHOME PYTHONPATH

uv_cmd=$(command -v uv 2>/dev/null || :)
pipx_cmd=$(command -v pipx 2>/dev/null || :)
if [ -z "$uv_cmd" ] && [ -x "$HOME/.local/bin/uv" ]; then uv_cmd=$HOME/.local/bin/uv; fi
if [ -z "$pipx_cmd" ] && [ -x "$HOME/.local/bin/pipx" ]; then pipx_cmd=$HOME/.local/bin/pipx; fi
uv_root=
uv_bin=
pipx_root=
pipx_bin=
uv_installed=no
pipx_installed=no
if [ -n "$uv_cmd" ]; then
    uv_root=$("$uv_cmd" tool dir) || fail "Cannot inspect uv tool environments"
    uv_bin=$("$uv_cmd" tool dir --bin) || fail "Cannot inspect uv command directory"
    [ ! -f "$uv_root/todogc/uv-receipt.toml" ] || uv_installed=yes
fi
if [ -n "$pipx_cmd" ]; then
    pipx_root=$("$pipx_cmd" environment --value PIPX_HOME) || fail "Cannot inspect pipx environments"
    pipx_bin=$("$pipx_cmd" environment --value PIPX_BIN_DIR) || fail "Cannot inspect pipx command directory"
    [ ! -f "$pipx_root/venvs/todogc/pipx_metadata.json" ] || pipx_installed=yes
fi
[ "$uv_installed:$pipx_installed" != yes:yes ] || fail "Both uv and pipx manage TodoGC. Keep one installation before retrying; no files were replaced."

existing=$(command -v todogc 2>/dev/null || :)
if [ -n "$existing" ] && [ "$uv_installed:$pipx_installed" = no:no ]; then
    fail "Another todogc owns the command: $existing. Resolve that installation before retrying."
fi
manager=uv
if [ "$pipx_installed" = yes ]; then manager=pipx; fi
if [ "$manager" = uv ] && [ -z "$uv_cmd" ]; then
    [ "$offline" = no ] || fail "Offline installation needs an existing uv or pipx TodoGC installation"
    command -v curl >/dev/null 2>&1 || fail "Install uv first, or install curl to download its official installer"
    download_dir=$(mktemp -d) || fail "Cannot create download directory"
    trap 'rm -rf "$download_dir"' EXIT HUP INT TERM
    curl --proto '=https' --tlsv1.2 -LsSf https://astral.sh/uv/install.sh -o "$download_dir/uv-install.sh"
    UV_INSTALL_DIR="$HOME/.local/bin" sh "$download_dir/uv-install.sh"
    uv_cmd=$HOME/.local/bin/uv
    [ -x "$uv_cmd" ] || fail "uv installer did not create $uv_cmd"
    uv_root=$("$uv_cmd" tool dir)
    uv_bin=$("$uv_cmd" tool dir --bin)
fi
if [ "$manager" = uv ]; then
    bin_dir=$uv_bin
    internal_cli=$uv_root/todogc/bin/todogc
    installed=$uv_installed
else
    bin_dir=$pipx_bin
    internal_cli=$pipx_root/venvs/todogc/bin/todogc
    installed=$pipx_installed
fi
case "$bin_dir:$internal_cli" in /*:/*) ;; *) fail "Tool manager returned non-absolute paths" ;; esac
check_persistable_path "$bin_dir"
cli=$bin_dir/todogc

# Refuse to mask, overwrite, or silently switch an unrelated installation.
# Both common managers expose symlinks on Unix; -ef also accepts hard links.
if [ -e "$cli" ] || [ -L "$cli" ]; then
    [ "$installed" = yes ] && [ -f "$internal_cli" ] && [ "$cli" -ef "$internal_cli" ] ||
        fail "Another command owns $cli. Resolve that installation before retrying."
fi
existing=$(command -v todogc 2>/dev/null || :)
if [ -n "$existing" ]; then
    [ "$installed" = yes ] && [ -f "$internal_cli" ] && [ "$existing" -ef "$internal_cli" ] ||
        fail "Another todogc shadows the user command: $existing. Remove that PATH conflict before retrying."
fi

if [ "$offline" = yes ]; then
    UV_OFFLINE=1
    PIP_NO_INDEX=1
    export UV_OFFLINE PIP_NO_INDEX
fi
if [ "$manager" = uv ]; then
    if [ "$installed" = yes ] && [ "$source_explicit" = no ]; then
        # Upgrade preserves the original extras, interpreter and tool requirements.
        "$uv_cmd" tool upgrade todogc
    elif [ "$installed" = no ]; then
        "$uv_cmd" tool install --python '>=3.10' --upgrade -- "$source_spec"
    else
        "$uv_cmd" tool install --upgrade -- "$source_spec"
    fi
else
    if [ "$source_explicit" = no ]; then
        "$pipx_cmd" upgrade todogc
    else
        # Current pipx supports a source change without --force or uninstalling first.
        "$pipx_cmd" install --upgrade -- "$source_spec"
    fi
fi
[ -x "$cli" ] && [ -f "$internal_cli" ] && [ "$cli" -ef "$internal_cli" ] ||
    fail "The manager did not expose its TodoGC command at $cli"

# Preserve explicit paths as individual arguments, including spaces and shell syntax.
set --
[ -z "$database" ] || set -- "$@" --db "$database"
[ -z "$config" ] || set -- "$@" --config "$config"
"$cli" "$@" setup --json
"$cli" "$@" skill install --target "$target" --json

# Delegate quoting and shell-specific persistence to the tool manager.
# Do this before prepending PATH: update-shell must see that persistence is needed.
update_path=
old_ifs=$IFS
IFS=:
for entry in $PATH; do
    [ "$entry" != "$bin_dir" ] || continue
    update_path=${update_path:+$update_path:}$entry
done
IFS=$old_ifs
if [ "$manager" = uv ]; then
    if update_output=$(PATH=$update_path "$uv_cmd" tool update-shell 2>&1); then
        printf '%s\n' "$update_output"
    else
        update_status=$?
        # uv returns 2 when the startup file already contains the correct entry
        # but this (older) process has not reloaded it. That is an idempotent
        # installation, unlike unsupported shells or an unwritable profile.
        case "$update_status:$update_output" in
            2:*"not in PATH"*"configuration files are already up-to-date"*)
                printf '%s\n' "Shell configuration already contains the tool directory; restart the terminal to load it." ;;
            *) printf '%s\n' "$update_output" >&2; exit "$update_status" ;;
        esac
    fi
else
    PATH=$update_path "$pipx_cmd" ensurepath
fi
PATH=$bin_dir:$PATH
export PATH
# Exit the checkout without discarding the selected user paths.
(cd "$HOME" && todogc --help >/dev/null)
"$cli" "$@" doctor --runtime --json
printf '%s\n' "Installed TodoGC using $manager: $cli" \
    "Command-name check passed outside the project without an activated environment." \
    "Open a new terminal (or restart your terminal app) to load the persistent PATH, then run: todogc doctor --runtime" \
    "An independently opened terminal has not been verified by this installer."
