import asyncio
import json
import types

import pytest

import db
from worker import clone


def make_msg(i, grouped=None):
    return types.SimpleNamespace(id=i, grouped_id=grouped, sticker=None, photo=object(), document=None, text="")


class FakeClient:
    """Just enough of TelegramClient for clone_group in forward mode."""

    def __init__(self, messages):
        self.messages = messages

    async def get_entity(self, ref):
        if ref == clone.DEST_GROUP_ID:
            return types.SimpleNamespace(id=999)
        return types.SimpleNamespace(id=42, title="Src", noforwards=False)

    async def get_messages(self, entity, ids=None, **kwargs):
        return next(m for m in self.messages if m.id == ids)

    async def iter_messages(self, entity, reverse=False, **kwargs):
        for m in self.messages:
            yield m


async def noop(text):
    pass


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "SENT_DB_PATH", tmp_path / "sent_media.json")
    monkeypatch.setattr(db, "SENT_DB_BACKUP", tmp_path / "sent_media.json.bak")
    monkeypatch.setattr(db, "_sent_db", {})
    monkeypatch.setattr(clone, "SEND_DELAY", 0)
    monkeypatch.setattr(clone, "PARALLEL_WORKERS", 1)
    monkeypatch.setattr(clone, "MAX_SCAN_MESSAGES", 10_000)

    async def topic(title):
        return 7

    monkeypatch.setattr(clone, "get_or_create_topic", topic)
    return tmp_path


def use_messages(monkeypatch, messages):
    monkeypatch.setattr(clone, "user_client", FakeClient(messages))


def test_cancel_keeps_progress(env, monkeypatch):
    forwarded = []

    async def fwd(src, dest, ids, thread_id):
        if forwarded:                      # 2nd call hangs until the job is cancelled
            await asyncio.sleep(60)
        forwarded.append(ids)

    monkeypatch.setattr(clone, "forward_to_topic", fwd)
    use_messages(monkeypatch, [make_msg(i) for i in range(1, 4)])

    async def run():
        task = asyncio.create_task(clone.clone_group("src", noop))
        while not forwarded:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert json.loads((env / "sent_media.json").read_text()) == {"42": [1]}


def test_progress_is_checkpointed_during_run(env, monkeypatch):
    calls = []
    snapshot = {}

    async def fwd(src, dest, ids, thread_id):
        calls.append(ids)
        if len(calls) == 11:               # before the 11th batch, 10 batches must already be on disk
            snapshot["data"] = json.loads((env / "sent_media.json").read_text())

    monkeypatch.setattr(clone, "forward_to_topic", fwd)
    use_messages(monkeypatch, [make_msg(i) for i in range(1, 13)])
    asyncio.run(clone.clone_group("src", noop))
    assert sorted(snapshot["data"]["42"]) == list(range(1, 11))

def test_scan_limit_counts_only_new_media(env, monkeypatch):
    monkeypatch.setattr(clone, "MAX_SCAN_MESSAGES", 10)
    db._sent_db["42"] = set(range(1, 21))        # 20 already sent; the limit must not be eaten by them
    forwarded = []

    async def fwd(src, dest, ids, thread_id):
        forwarded.append(ids)

    monkeypatch.setattr(clone, "forward_to_topic", fwd)
    use_messages(monkeypatch, [make_msg(i) for i in range(1, 26)])
    asyncio.run(clone.clone_group("src", noop))
    assert sorted(i for ids in forwarded for i in ids) == [21, 22, 23, 24, 25]


def test_limit_does_not_split_an_album_and_tells_user(env, monkeypatch):
    monkeypatch.setattr(clone, "MAX_SCAN_MESSAGES", 3)
    forwarded, statuses = [], []

    async def fwd(src, dest, ids, thread_id):
        forwarded.append(ids)

    async def status(text):
        statuses.append(text)

    monkeypatch.setattr(clone, "forward_to_topic", fwd)
    use_messages(monkeypatch, [make_msg(1), make_msg(2), make_msg(3, 9), make_msg(4, 9), make_msg(5), make_msg(6)])
    asyncio.run(clone.clone_group("src", status))
    assert forwarded == [[1], [2], [3, 4]]
    assert any("MAX_SCAN_MESSAGES" in t for t in statuses)


def test_single_message_is_recorded_as_sent(env, monkeypatch):
    forwarded = []

    async def fwd(src, dest, ids, thread_id):
        forwarded.append(ids)

    monkeypatch.setattr(clone, "forward_to_topic", fwd)
    use_messages(monkeypatch, [make_msg(i) for i in range(1, 4)])
    asyncio.run(clone.clone_single_message("src", 2, noop))
    assert forwarded == [[2]]
    assert db.get_sent("42") == {2}                       # a later group clone will skip it
    assert json.loads((env / "sent_media.json").read_text()) == {"42": [2]}
