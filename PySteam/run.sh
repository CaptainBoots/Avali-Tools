#!/usr/bin/env bash
set -e

REQUIRED_PKGS=(python-pyqt6)
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

if ! command -v steam &>/dev/null; then
    echo "Warning: 'steam' binary not found on PATH. Games can't be launched without it."
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/main.py" "$@"
