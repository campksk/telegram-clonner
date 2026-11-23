import asyncio
import json
import types

import pytest

import db
from worker import clone


def make_msg(i, grouped=None, text=""):
    return types.SimpleNamespace(id=i, grouped_id=grouped, sticker=None, photo=object(), document=None, text=text)


class FakeClient:
    """Just enough of TelegramClient for clone_group in forward mode."""

    def __init__(self, messages, noforwards=False):
        self.messages = messages
        self.noforwards = noforwards
        self.sent = []          # ("file", file, caption) / ("msg", text)

    async def get_entity(self, ref):
        if ref == clone.DEST_GROUP_ID:
            return types.SimpleNamespace(id=999)
        return types.SimpleNamespace(id=42, title="Src", noforwards=self.noforwards)

    async def download_media(self, msg, file=None):
        path = f"{file}{msg.id}.jpg"
        with open(path, "wb") as f:
            f.write(b"x")
        return path

    async def send_file(self, dest, file=None, caption=None, reply_to=None, parse_mode=None):
        self.sent.append(("file", [p.rsplit("/", 1)[-1] for p in file] if isinstance(file, list) else file.rsplit("/", 1)[-1], caption))

    async def send_message(self, dest, text, reply_to=None, parse_mode=None):
        self.sent.append(("msg", text))

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


def test_album_caption_comes_from_the_message_that_has_it(env, monkeypatch):
    monkeypatch.setattr(clone, "DOWNLOAD_DIR", env)
    client = FakeClient([make_msg(1, grouped=9, text="album caption"), make_msg(2, grouped=9), make_msg(3, text="single")], noforwards=True)
    monkeypatch.setattr(clone, "user_client", client)
    asyncio.run(clone.clone_group("src", noop))
    assert client.sent == [("file", ["1.jpg", "2.jpg"], ["album caption", ""]), ("file", "3.jpg", "single")]
    assert db.get_sent("42") == {1, 2, 3}


def test_oversized_caption_does_not_fail_the_upload(env, monkeypatch):
    monkeypatch.setattr(clone, "DOWNLOAD_DIR", env)
    big = "B" * 2000
    client = FakeClient([make_msg(1, text=big)], noforwards=True)
    monkeypatch.setattr(clone, "user_client", client)
    asyncio.run(clone.clone_group("src", noop))
    assert client.sent == [("file", "1.jpg", ""), ("msg", big)]
    assert db.get_sent("42") == {1}
