#!/usr/bin/env bash
#
# Checks for required system packages and installs any that are missing,
# then launches the app. Written for Arch-based distros (CachyOS included),
# since that's what pacman/python-pyqt6 assumes.

set -e

REQUIRED_PKGS=(python-pyqt6 python-pyqt6-webengine)
MISSING=()

for pkg in "${REQUIRED_PKGS[@]}"; do
    if ! pacman -Qi "$pkg" &>/dev/null; then
        MISSING+=("$pkg")
    fi
done

if [ ${#MISSING[@]} -ne 0 ]; then
    echo "Missing required packages: ${MISSING[*]}"
    echo "Installing now (will prompt for your sudo password)..."
    sudo pacman -S --needed --noconfirm "${MISSING[@]}"
else
    echo "All required packages already installed."
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/main.py" "$@"
