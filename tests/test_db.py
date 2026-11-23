import asyncio
import json

import db


def test_save_sent_concurrent_calls_stay_valid(tmp_path, monkeypatch):
    path = tmp_path / "sent_media.json"
    monkeypatch.setattr(db, "SENT_DB_PATH", path)
    monkeypatch.setattr(db, "SENT_DB_BACKUP", tmp_path / "sent_media.json.bak")
    monkeypatch.setattr(db, "_sent_db", {"1": set(range(5000))})

    async def run():
        await asyncio.gather(*[db.save_sent() for _ in range(20)])

    asyncio.run(run())
    assert len(json.loads(path.read_text())["1"]) == 5000
    assert not list(tmp_path.glob("*.tmp"))


def test_failed_write_keeps_previous_file(tmp_path, monkeypatch):
    path = tmp_path / "sent_media.json"
    monkeypatch.setattr(db, "SENT_DB_PATH", path)
    monkeypatch.setattr(db, "SENT_DB_BACKUP", tmp_path / "sent_media.json.bak")
    monkeypatch.setattr(db, "_sent_db", {"1": {1}})
    asyncio.run(db.save_sent())

    db._sent_db["1"].add(2)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(db.os, "replace", boom)
    asyncio.run(db.save_sent())                      # error is logged, not raised
    monkeypatch.undo()
    assert json.loads(path.read_text()) == {"1": [1]}
    assert not list(tmp_path.glob("*.tmp"))


def test_pending_jobs_keep_typed_ref(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "JOBS_DB_PATH", tmp_path / "pending_jobs.json")
    db.add_pending_job("https://t.me/c/1234567", -1001234567)
    db.add_pending_job("durov", "durov")
    db.add_pending_job("legacy")                      # no ref
    db.add_pending_job("durov", "durov")              # duplicate ignored
    jobs = db.load_pending_jobs()
    assert jobs == [
        {"source": "https://t.me/c/1234567", "ref": -1001234567},
        {"source": "durov", "ref": "durov"},
        {"source": "legacy"},
    ]
    db.remove_pending_job("durov")
    assert [j["source"] for j in db.load_pending_jobs()] == ["https://t.me/c/1234567", "legacy"]


def test_mark_url_sent_persists_atomically(tmp_path, monkeypatch):
    path = tmp_path / "sent_urls.json"
    monkeypatch.setattr(db, "URLS_DB_PATH", path)
    monkeypatch.setattr(db, "_sent_urls", set())
    asyncio.run(db.mark_url_sent("https://x.com/a/status/1"))
    assert json.loads(path.read_text()) == {"urls": ["https://x.com/a/status/1"]}
    assert not list(tmp_path.glob("*.tmp"))
