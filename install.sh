#!/usr/bin/env bash
# Links this plugin folder into StreamController's plugin directory.
#
# Works for both the Flatpak (Flathub) install and a native/pip install of
# StreamController. Re-run after pulling updates - it just re-creates the
# symlink, your settings stay untouched (they live elsewhere).
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ID="dev_enjxz_GrokUsage"

FLATPAK_DATA_DIR="$HOME/.var/app/com.core447.StreamController/data"
NATIVE_DATA_DIR="$HOME/.local/share/StreamController"

if [ -d "$FLATPAK_DATA_DIR" ]; then
    DATA_DIR="$FLATPAK_DATA_DIR"
elif [ -d "$NATIVE_DATA_DIR" ]; then
    DATA_DIR="$NATIVE_DATA_DIR"
else
    echo "Could not find a StreamController data directory." >&2
    echo "Looked for:" >&2
    echo "  - $FLATPAK_DATA_DIR (Flatpak)" >&2
    echo "  - $NATIVE_DATA_DIR (native)" >&2
    echo "Start StreamController at least once, or set DEST manually and re-run." >&2
    exit 1
fi

DEST="$DATA_DIR/plugins/$PLUGIN_ID"
mkdir -p "$(dirname "$DEST")"

if [ -e "$DEST" ] && [ ! -L "$DEST" ]; then
    echo "Refusing to overwrite non-symlink at $DEST" >&2
    exit 1
fi

ln -sfn "$SRC_DIR" "$DEST"
echo "Linked $SRC_DIR"
echo "    -> $DEST"
echo "Restart StreamController, then add the 'Grok Usage' action to a key."
