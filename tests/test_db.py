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


# ---------- rolling backup and recovery ----------

def _paths(tmp_path, monkeypatch):
    main, bak = tmp_path / "sent_media.json", tmp_path / "sent_media.json.bak"
    monkeypatch.setattr(db, "SENT_DB_PATH", main)
    monkeypatch.setattr(db, "SENT_DB_BACKUP", bak)
    return main, bak


def test_backup_follows_the_previous_save(tmp_path, monkeypatch):
    main, bak = _paths(tmp_path, monkeypatch)
    monkeypatch.setattr(db, "_sent_db", {"1": {1}})
    asyncio.run(db.save_sent())
    assert not bak.exists()                               # nothing to back up yet
    db._sent_db["1"].add(2)
    asyncio.run(db.save_sent())
    assert json.loads(bak.read_text()) == {"1": [1]}      # the state before the last save
    assert sorted(json.loads(main.read_text())["1"]) == [1, 2]
    db._sent_db["1"].add(3)
    asyncio.run(db.save_sent())
    assert sorted(json.loads(bak.read_text())["1"]) == [1, 2]   # keeps following (used to stay at the first state forever)
    assert not list(tmp_path.glob("*.tmp"))


def test_corrupt_main_file_is_replaced_by_the_backup_and_kept_aside(tmp_path, monkeypatch):
    main, bak = _paths(tmp_path, monkeypatch)
    bak.write_text(json.dumps({"1": [1, 2, 3]}))
    main.write_text('{"1": [1, 2, 3, 4')                  # truncated, e.g. disk full / manual edit
    db.load_sent()
    assert db.get_sent("1") == {1, 2, 3}
    assert not main.exists() and (tmp_path / "sent_media.json.corrupt").read_text() == '{"1": [1, 2, 3, 4'

    db._sent_db["1"].add(9)
    asyncio.run(db.save_sent())                           # the damaged file must never overwrite the good backup
    assert json.loads(bak.read_text()) == {"1": [1, 2, 3]}
    assert sorted(json.loads(main.read_text())["1"]) == [1, 2, 3, 9]


def test_wrong_json_shape_counts_as_corrupt(tmp_path, monkeypatch):
    main, bak = _paths(tmp_path, monkeypatch)
    bak.write_text(json.dumps({"7": [5]}))
    main.write_text("[1, 2, 3]")
    db.load_sent()
    assert db.get_sent("7") == {5}


def test_both_files_unreadable_starts_empty(tmp_path, monkeypatch):
    main, bak = _paths(tmp_path, monkeypatch)
    main.write_text("garbage")
    bak.write_text("also garbage")
    db.load_sent()
    assert db._sent_db == {}


def test_deleting_the_main_file_resets_even_if_a_backup_exists(tmp_path, monkeypatch):
    main, bak = _paths(tmp_path, monkeypatch)
    bak.write_text(json.dumps({"1": [1]}))
    db.load_sent()
    assert db._sent_db == {}                              # no silent resurrection of old state


def test_healthy_file_loads_normally(tmp_path, monkeypatch):
    main, bak = _paths(tmp_path, monkeypatch)
    main.write_text(json.dumps({"1": [1, 2], "2": []}))
    db.load_sent()
    assert db.get_sent("1") == {1, 2} and db.total_tracked() == (2, 2)
