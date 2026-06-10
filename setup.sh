#!/bin/bash
# setup.sh — ติดตั้ง tg-clone-bot บน Raspberry Pi / Linux (Debian-based)

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"

echo "=== tg-clone-bot setup ==="

# 1. ตรวจ python3
if ! command -v python3 &>/dev/null; then
    echo "[ERROR] ไม่พบ python3 — รัน: sudo apt install python3 python3-venv python3-full"
    exit 1
fi

# 2. สร้าง venv (ถ้ายังไม่มี)
if [ ! -d "$VENV_DIR" ]; then
    echo "[1/3] สร้าง virtual environment..."
    python3 -m venv "$VENV_DIR"
else
    echo "[1/3] พบ venv อยู่แล้ว — ข้าม"
fi

# 3. ติดตั้ง dependencies
echo "[2/3] ติดตั้ง packages..."
"$VENV_DIR/bin/pip" install --upgrade pip --quiet
"$VENV_DIR/bin/pip" install -r "$SCRIPT_DIR/requirements.txt" --quiet
echo "      ติดตั้งสำเร็จ"

# 4. สร้าง run.sh
cat > "$SCRIPT_DIR/run.sh" << EOF
#!/bin/bash
cd "$SCRIPT_DIR"
exec "$VENV_DIR/bin/python" main.py
EOF
chmod +x "$SCRIPT_DIR/run.sh"
echo "[3/3] สร้าง run.sh สำเร็จ"

echo ""
echo "=== Setup เสร็จสิ้น ==="
echo ""
echo "ขั้นตอนต่อไป:"
echo "  1. แก้ไข .env  →  cp .env.example .env && nano .env"
echo "  2. รันครั้งแรก (login User client)  →  ./run.sh"
echo "  3. หลัง login แล้ว ติดตั้ง systemd service:"
echo "       sudo cp tg-clone-bot.service /etc/systemd/system/"
echo "       sudo systemctl daemon-reload"
echo "       sudo systemctl enable --now tg-clone-bot"
