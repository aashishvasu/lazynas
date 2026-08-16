#!/usr/bin/env bash
# Install lazynas into an isolated venv and shim it onto the system PATH.
#
# Isolated stdlib venv instead of pipx/uv: no PEP 668 conflict on modern
# distros, nothing extra to install first, and the shim lives on root's PATH
# so `sudo lazynas` resolves (pipx installs per-user, which sudo would miss).
set -euo pipefail

VENV="${LAZYNAS_VENV:-/usr/lib/lazynas/venv}"
SHIM="${LAZYNAS_SHIM:-/usr/local/bin/lazynas}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

need_root() {
    if [ "$(id -u)" -ne 0 ]; then
        echo "install.sh writes $VENV and $SHIM; re-run with sudo" >&2
        exit 1
    fi
}

find_python() {
    # ensurepip is checked too: Debian/Ubuntu split it into python3-venv,
    # and without it `python3 -m venv` fails halfway through the install.
    for cand in python3.14 python3.13 python3.12 python3.11 python3; do
        if command -v "$cand" >/dev/null 2>&1 &&
            "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' &&
            "$cand" -c 'import ensurepip' 2>/dev/null; then
            echo "$cand"
            return 0
        fi
    done
    return 1
}

install() {
    need_root
    local py
    if ! py="$(find_python)"; then
        echo "need Python 3.11+ with the venv module; none found on PATH" >&2
        echo "on Debian/Ubuntu: apt install python3-venv" >&2
        exit 1
    fi
    echo "using $("$py" --version) at $(command -v "$py")"

    rm -rf "$VENV"
    "$py" -m venv "$VENV"
    "$VENV/bin/pip" install --quiet --upgrade pip
    "$VENV/bin/pip" install --quiet "$SRC"

    mkdir -p "$(dirname "$SHIM")"
    printf '#!/bin/sh\nexec %s/bin/lazynas "$@"\n' "$VENV" >"$SHIM"
    chmod 0755 "$SHIM"

    echo "installed: $("$SHIM" --version)"
    echo "try: lazynas disks"
}

uninstall() {
    need_root
    rm -rf "$VENV"
    rm -f "$SHIM"
    echo "removed $VENV and $SHIM"
}

case "${1:-install}" in
    install) install ;;
    uninstall | --uninstall | -u) uninstall ;;
    *)
        echo "usage: install.sh [install|uninstall]" >&2
        exit 2
        ;;
esac
