"""
db.py — persistence layer สำหรับ sent_media

เก็บ msg_id ที่ส่งสำเร็จแล้วแต่ละ source ไว้ใน JSON
เพื่อป้องกันการส่งซ้ำเมื่อ clone ครั้งถัดไป
"""

import json
import logging

from config import SENT_DB_PATH

log = logging.getLogger("clone-bot.db")

# in-memory store: {source_entity_id_str: set(msg_id, ...)}
_db: dict[str, set[int]] = {}


def load() -> None:
    """โหลดข้อมูลจากไฟล์เข้า memory (เรียกตอน startup)"""
    global _db
    if not SENT_DB_PATH.exists():
        _db = {}
        return
    try:
        raw = json.loads(SENT_DB_PATH.read_text(encoding="utf-8"))
        _db = {k: set(v) for k, v in raw.items()}
        log.info(f"โหลด sent_db สำเร็จ ({sum(len(v) for v in _db.values())} records)")
    except Exception as e:
        log.warning(f"โหลด sent_db ล้มเหลว ใช้ค่าว่าง: {e}")
        _db = {}


def save() -> None:
    """บันทึก memory ลงไฟล์"""
    try:
        SENT_DB_PATH.write_text(
            json.dumps({k: list(v) for k, v in _db.items()}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        log.error(f"บันทึก sent_db ล้มเหลว: {e}")


def get_sent(src_key: str) -> set[int]:
    """คืน set ของ msg_id ที่ส่งไปแล้วของ source นั้น"""
    return _db.get(src_key, set())


def mark_sent(src_key: str, msg_ids: set[int]) -> None:
    """บันทึก msg_ids ว่าส่งแล้ว และ save ทันที"""
    if src_key not in _db:
        _db[src_key] = set()
    _db[src_key].update(msg_ids)
    save()


def total_tracked() -> tuple[int, int]:
    """คืน (จำนวนไฟล์ทั้งหมด, จำนวน source)"""
    return sum(len(v) for v in _db.values()), len(_db)
