"""
database.py — จัดการ sent_media.json

เก็บ {source_entity_id: {msg_id, ...}} เพื่อไม่ส่งไฟล์ซ้ำ
"""

import json
import logging
from pathlib import Path

log = logging.getLogger("clone-bot.db")

SENT_DB = Path("./sent_media.json")

# ─── In-memory store (โหลดครั้งเดียวตอน import) ────────────────────────────────
_db: dict[str, set[int]] = {}


def _load() -> dict[str, set[int]]:
    if not SENT_DB.exists():
        return {}
    try:
        raw = json.loads(SENT_DB.read_text(encoding="utf-8"))
        return {k: set(v) for k, v in raw.items()}
    except Exception as e:
        log.warning(f"โหลด sent_db ล้มเหลว: {e} — เริ่มใหม่")
        return {}


def _save() -> None:
    try:
        SENT_DB.write_text(
            json.dumps({k: list(v) for k, v in _db.items()}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        log.error(f"บันทึก sent_db ล้มเหลว: {e}")


# โหลดทันทีที่ import
_db = _load()
log.info(f"sent_db โหลดแล้ว: {len(_db)} source, {sum(len(v) for v in _db.values())} ไฟล์")


# ─── Public API ────────────────────────────────────────────────────────────────
def get_sent(source_id: int) -> set[int]:
    """คืน set ของ msg_id ที่เคยส่งไปแล้วสำหรับ source นี้"""
    return _db.get(str(source_id), set())


def mark_sent(source_id: int, msg_ids: set[int]) -> None:
    """บันทึก msg_ids ว่าส่งไปแล้ว และ persist ลงดิสก์ทันที"""
    key = str(source_id)
    existing = _db.get(key, set())
    existing.update(msg_ids)
    _db[key] = existing
    _save()


def total_tracked() -> tuple[int, int]:
    """คืน (จำนวน source, จำนวนไฟล์ทั้งหมด)"""
    return len(_db), sum(len(v) for v in _db.values())
