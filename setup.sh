#!/bin/bash
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"
echo "=== tg-clone-bot setup ==="
if ! command -v python3 &>/dev/null; then echo "[ERROR] python3 not found"; exit 1; fi
if [ ! -d "$VENV_DIR" ]; then python3 -m venv "$VENV_DIR"; fi
"$VENV_DIR/bin/pip" install -r "$SCRIPT_DIR/requirements.txt" --quiet

# Verify gallery-dl is installed and accessible
"$VENV_DIR/bin/gallery-dl" --version || { echo "[ERROR] gallery-dl not found after install"; exit 1; }
echo "gallery-dl: OK"
cat > "$SCRIPT_DIR/run.sh" << EOF
#!/bin/bash
cd "$SCRIPT_DIR"
exec "$VENV_DIR/bin/python" main.py
EOF
chmod +x "$SCRIPT_DIR/run.sh"
echo "Setup complete"
