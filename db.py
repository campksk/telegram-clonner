import json
import logging
from pathlib import Path

log = logging.getLogger("clone-bot.db")

SENT_DB_PATH = Path("./sent_media.json")
JOBS_DB_PATH = Path("./pending_jobs.json")

_sent_db: dict[str, set[int]] = {}

def load_sent() -> None:
    global _sent_db
    if not SENT_DB_PATH.exists():
        _sent_db = {}
        return
    try:
        raw = json.loads(SENT_DB_PATH.read_text(encoding="utf-8"))
        _sent_db = {k: set(v) for k, v in raw.items()}
        log.info(f"โหลด sent_db สำเร็จ ({sum(len(v) for v in _sent_db.values())} records)")
    except Exception as e:
        log.warning(f"โหลด sent_db ล้มเหลว ใช้ค่าว่าง: {e}")
        _sent_db = {}

def save_sent() -> None:
    try:
        SENT_DB_PATH.write_text(
            json.dumps({k: list(v) for k, v in _sent_db.items()}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        log.error(f"บันทึก sent_db ล้มเหลว: {e}")

def get_sent(src_key: str) -> set[int]:
    return _sent_db.get(src_key, set())

def mark_sent(src_key: str, msg_ids: set[int]) -> None:
    if src_key not in _sent_db:
        _sent_db[src_key] = set()
    _sent_db[src_key].update(msg_ids)
    save_sent()

def total_tracked() -> tuple[int, int]:
    return sum(len(v) for v in _sent_db.values()), len(_sent_db)

def load_pending_jobs() -> list[dict]:
    if not JOBS_DB_PATH.exists():
        return []
    try:
        return json.loads(JOBS_DB_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []

def save_pending_jobs(jobs: list[dict]) -> None:
    JOBS_DB_PATH.write_text(json.dumps(jobs, ensure_ascii=False), encoding="utf-8")

def add_pending_job(source_arg: str) -> None:
    jobs = load_pending_jobs()
    if not any(str(j.get("source")) == str(source_arg) for j in jobs):
        jobs.append({"source": source_arg})
        save_pending_jobs(jobs)

def remove_pending_job(source_arg: str) -> None:
    jobs = [j for j in load_pending_jobs() if str(j.get("source")) != str(source_arg)]
    save_pending_jobs(jobs)
