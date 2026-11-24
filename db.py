import json
import logging
import os
import shutil
from pathlib import Path


log = logging.getLogger("clone-bot.db")

SENT_DB_PATH = Path("./sent_media.json")
SENT_DB_BACKUP = Path("./sent_media.json.bak")
JOBS_DB_PATH = Path("./pending_jobs.json")
URLS_DB_PATH = Path("./sent_urls.json")

_sent_db: dict[str, set[int]] = {}
_sent_urls: set[str] = set()


def _read_sent(path: Path) -> dict[str, set[int]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("expected a JSON object")
    return {str(k): {int(i) for i in v} for k, v in raw.items()}


def load_sent() -> None:
    """Load the sent-message database.

    If the main file exists but is unreadable it is set aside as '<name>.corrupt' (so it can never be
    copied over the backup) and the last backup is used instead. A missing main file means 'start
    fresh' - the backup is not used then, so deleting the file really does reset it.
    """
    global _sent_db
    _sent_db = {}
    if not SENT_DB_PATH.exists():
        return
    try:
        _sent_db = _read_sent(SENT_DB_PATH)
        log.info(f"Loaded sent_db ({sum(len(v) for v in _sent_db.values())} records)")
        return
    except Exception as e:
        log.error(f"{SENT_DB_PATH.name} is unreadable ({e})")

    corrupt = SENT_DB_PATH.with_name(SENT_DB_PATH.name + ".corrupt")
    try:
        os.replace(SENT_DB_PATH, corrupt)
        log.error(f"damaged file kept as {corrupt.name}")
    except OSError as e:
        log.error(f"could not move the damaged file aside: {e}")

    if SENT_DB_BACKUP.exists():
        try:
            _sent_db = _read_sent(SENT_DB_BACKUP)
            log.warning(f"restored sent_db from {SENT_DB_BACKUP.name} ({sum(len(v) for v in _sent_db.values())} records); "
                        "the last few messages sent before the damage may be sent again")
            return
        except Exception as e:
            log.error(f"{SENT_DB_BACKUP.name} is unreadable too ({e})")
    log.error("starting with an empty sent_db")
    _sent_db = {}


def _write_atomic(path: Path, data: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _refresh_backup() -> None:
    """Keep the previous (known-good, because writes are atomic) file as the rolling backup."""
    if not SENT_DB_PATH.exists():
        return
    tmp = SENT_DB_BACKUP.with_name(SENT_DB_BACKUP.name + ".tmp")
    try:
        shutil.copyfile(SENT_DB_PATH, tmp)
        os.replace(tmp, SENT_DB_BACKUP)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        log.warning(f"could not refresh {SENT_DB_BACKUP.name}: {e}")


async def save_sent() -> None:
    _refresh_backup()
    try:
        data = json.dumps({k: list(v) for k, v in _sent_db.items()}, ensure_ascii=False)
        # Written synchronously on purpose: there is no await between serialising and replacing
        # the file, so concurrent jobs can never interleave writes, and a crash mid-write
        # leaves the previous file intact (write to a temp file, then atomic replace).
        _write_atomic(SENT_DB_PATH, data)
    except Exception as e:
        log.error(f"Failed to save sent_db: {e}")


def get_sent(src_key: str) -> set[int]:
    return _sent_db.get(src_key, set())


def mark_sent_sync(src_key: str, msg_ids: set[int]) -> None:
    """Update in-memory only (no I/O). Call save_sent() later to persist."""
    if src_key not in _sent_db:
        _sent_db[src_key] = set()
    _sent_db[src_key].update(msg_ids)


async def mark_sent(src_key: str, msg_ids: set[int]) -> None:
    """Update + persist immediately."""
    mark_sent_sync(src_key, msg_ids)
    await save_sent()


def total_tracked() -> tuple[int, int]:
    return sum(len(v) for v in _sent_db.values()), len(_sent_db)


# --- URL dedup ---

def load_sent_urls() -> None:
    global _sent_urls
    if not URLS_DB_PATH.exists():
        _sent_urls = set()
        return
    try:
        raw = json.loads(URLS_DB_PATH.read_text(encoding="utf-8"))
        _sent_urls = set(raw.get("urls", []))
        log.info(f"Loaded sent_urls ({len(_sent_urls)} urls)")
    except Exception:
        _sent_urls = set()


def get_sent_urls() -> set[str]:
    return _sent_urls


async def mark_url_sent(url: str) -> None:
    _sent_urls.add(url)
    try:
        data = json.dumps({"urls": sorted(_sent_urls)}, ensure_ascii=False)
        _write_atomic(URLS_DB_PATH, data)
    except Exception as e:
        log.error(f"Failed to save sent_urls: {e}")


# --- Pending jobs ---

def load_pending_jobs() -> list[dict]:
    if not JOBS_DB_PATH.exists():
        return []
    try:
        return json.loads(JOBS_DB_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_pending_jobs(jobs: list[dict]) -> None:
    _write_atomic(JOBS_DB_PATH, json.dumps(jobs, ensure_ascii=False))


def add_pending_job(source_arg: str, ref=None) -> None:
    """Remember a running group clone so it can resume after a restart.

    `source_arg` is the key (what the user typed); `ref` is the parsed source (int ID or username),
    stored so resume doesn't have to re-parse text such as a private t.me/c/ link.
    """
    jobs = load_pending_jobs()
    if not any(str(j.get("source")) == str(source_arg) for j in jobs):
        job = {"source": source_arg}
        if ref is not None:
            job["ref"] = ref
        jobs.append(job)
        save_pending_jobs(jobs)


def remove_pending_job(source_arg: str) -> None:
    jobs = [j for j in load_pending_jobs() if str(j.get("source")) != str(source_arg)]
    save_pending_jobs(jobs)
