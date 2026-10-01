#!/bin/bash
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"
echo "=== tg-clone-bot setup ==="
if ! command -v python3 &>/dev/null; then echo "[ERROR] ไม่พบ python3"; exit 1; fi
if [ ! -d "$VENV_DIR" ]; then python3 -m venv "$VENV_DIR"; fi
"$VENV_DIR/bin/pip" install -r "$SCRIPT_DIR/requirements.txt" --quiet
cat > "$SCRIPT_DIR/run.sh" << EOF
#!/bin/bash
cd "$SCRIPT_DIR"
exec "$VENV_DIR/bin/python" main.py
EOF
chmod +x "$SCRIPT_DIR/run.sh"
echo "Setup เสร็จสิ้น"
