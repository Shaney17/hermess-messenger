#!/usr/bin/env bash
#
# Installer for the Hermes Messenger gateway plugin.
#
# Copies the plugin into the USER plugin dir ($HERMES_HOME/plugins/messenger)
# so `hermes update` never clobbers it, then enables it in config.yaml.
# Run from anywhere; it locates files relative to this script.
#
# Usage:
#   ./install.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$SCRIPT_DIR/messenger"

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
DEST_DIR="$HERMES_HOME/plugins"
DEST="$DEST_DIR/messenger"
CONFIG="$HERMES_HOME/config.yaml"

echo "Hermes Messenger plugin installer"
echo "---------------------------------"
echo "HERMES_HOME : $HERMES_HOME"
echo "Install to  : $DEST"
echo

if [ ! -d "$SRC" ]; then
  echo "✗ Could not find plugin source at $SRC" >&2
  exit 1
fi

if [ ! -d "$HERMES_HOME" ]; then
  echo "✗ $HERMES_HOME does not exist. Install/run Hermes Agent first," >&2
  echo "  or set HERMES_HOME to your Hermes home directory." >&2
  exit 1
fi

# 1. Copy the plugin package.
mkdir -p "$DEST_DIR"
rm -rf "$DEST"
cp -R "$SRC" "$DEST"
echo "✓ Copied plugin to $DEST"

# 2. Enable it in config.yaml (idempotent merge of plugins.enabled).
python3 - "$CONFIG" <<'PY'
import sys, os

path = sys.argv[1]
try:
    import yaml
except ImportError:
    print("⚠ PyYAML not available — add this to %s manually:" % path)
    print("    plugins:\n      enabled:\n        - messenger")
    sys.exit(0)

data = {}
if os.path.exists(path):
    with open(path) as f:
        data = yaml.safe_load(f) or {}

plugins = data.setdefault("plugins", {})
enabled = plugins.get("enabled")
if not isinstance(enabled, list):
    enabled = []
if "messenger" not in enabled:
    enabled.append("messenger")
plugins["enabled"] = enabled

with open(path, "w") as f:
    yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
print("✓ Enabled 'messenger' in %s (plugins.enabled)" % path)
PY

echo
echo "Next steps:"
echo "  1. Set the required secrets in $HERMES_HOME/.env :"
echo "       MESSENGER_PAGE_ACCESS_TOKEN=..."
echo "       MESSENGER_APP_SECRET=..."
echo "       MESSENGER_VERIFY_TOKEN=<any-random-string>"
echo "     (or run: hermes setup messenger)"
echo "  2. Expose the webhook over HTTPS (e.g. cloudflared tunnel --url http://localhost:8650)."
echo "  3. In the Meta console set the Callback URL to <public-url>/messenger/webhook,"
echo "     paste the same Verify Token, subscribe fields 'messages' + 'messaging_postbacks',"
echo "     and subscribe your Page."
echo "  4. Start the gateway and confirm:  hermes gateway status   (look for Messenger 💬)"
echo
echo "Done."
