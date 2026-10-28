#!/usr/bin/env bash
# Kurt installer for macOS / Linux
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
KHOME="$HOME/.kurt"
BIN="$HOME/.local/bin"

if ! command -v python3 >/dev/null 2>&1; then
  echo "❌ python3 not found. Install Python 3.9+ from https://www.python.org/downloads/ and re-run."
  exit 1
fi

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
  echo "❌ Python 3.9 or newer is required (found: $(python3 --version 2>&1))."
  echo "   Install a newer Python from https://www.python.org/downloads/ and re-run."
  exit 1
fi

echo "→ Creating private environment in $KHOME/venv"
if ! python3 -m venv "$KHOME/venv"; then
  echo "❌ Could not create a virtual environment."
  echo "   On Debian/Ubuntu run: sudo apt install python3-venv   then re-run this installer."
  exit 1
fi
"$KHOME/venv/bin/python" -m pip install --upgrade pip -q
echo "→ Installing Kurt"
"$KHOME/venv/bin/python" -m pip install "$DIR" -q

mkdir -p "$BIN"
ln -sf "$KHOME/venv/bin/kurt" "$BIN/kurt"

case ":$PATH:" in
  *":$BIN:"*) ;;
  *)
    LINE='export PATH="$HOME/.local/bin:$PATH"'
    for rc in "$HOME/.zshrc" "$HOME/.bash_profile"; do
      if [ -f "$rc" ] || [ "$rc" = "$HOME/.zshrc" ]; then
        grep -qs '.local/bin' "$rc" || echo "$LINE" >> "$rc"
      fi
    done
    echo "→ Added $BIN to your PATH (in ~/.zshrc)"
    ;;
esac

echo
echo "✅ Kurt installed."
echo "   Open a NEW terminal window, cd into any project, and run:  kurt"