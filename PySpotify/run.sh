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

# Widevine CDM (audio): Spotify streams are DRM-encrypted. QtWebEngine
# auto-loads /usr/lib/chromium/libwidevinecdm.so when present.
if [ -f /usr/lib/chromium/libwidevinecdm.so ] \
    || [ -f /opt/google/chrome/libwidevinecdm.so ] \
    || [ -f /opt/google/chrome/WidevineCdm/_platform_specific/linux_x64/libwidevinecdm.so ] \
    || ls ~/.config/google-chrome/WidevineCdm >/dev/null 2>&1 \
    || ls ~/.config/chromium/WidevineCdm >/dev/null 2>&1; then
    echo "Widevine CDM found."
else
    echo "Widevine CDM not found - audio will fail (EMEError: No supported keysystem)."
    echo "  Install it: yay -S chromium-widevine   (or: yay -S google-chrome)"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/main.py" "$@"
