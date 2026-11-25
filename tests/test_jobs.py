import asyncio
import json
import types

import pytest

import config
import db
import jobs
from handlers import message, system
from parsing import MESSAGE_PATTERN


class FakeStatusMsg:
    def __init__(self, texts):
        self.texts = texts

    async def edit(self, text, parse_mode=None):
        self.texts.append(text)


class FakeEvent:
    """A private message from an allowed user. `texts` collects everything the user would see."""

    def __init__(self, text, sender_id=1):
        self.sender_id = sender_id
        self.is_private = True
        self.pattern_match = MESSAGE_PATTERN.match(text) or None
        self.texts = []

    async def reply(self, text, **kw):
        self.texts.append(text)
        return FakeStatusMsg(self.texts)

    async def respond(self, text, **kw):
        self.texts.append(text)


class CancelEvent(FakeEvent):
    def __init__(self, arg=""):
        super().__init__("/cancel " + arg)
        self.pattern_match = types.SimpleNamespace(group=lambda i: arg)


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    config.active_tasks.clear()
    monkeypatch.setattr(db, "JOBS_DB_PATH", tmp_path / "pending_jobs.json")
    monkeypatch.setattr(db, "URLS_DB_PATH", tmp_path / "sent_urls.json")
    monkeypatch.setattr(db, "_sent_urls", set())
    yield
    config.active_tasks.clear()


async def drain():
    tasks = list(config.active_tasks.values())
    await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.sleep(0)


# ---------- single message links are real, tracked jobs ----------

def test_single_message_job_is_tracked_cancellable_and_not_silent(monkeypatch):
    calls = []

    async def slow(chat, msg_id, status_cb):
        calls.append((chat, msg_id))
        await asyncio.sleep(60)

    monkeypatch.setattr(message.worker, "clone_single_message", slow)

    async def run():
        ev = FakeEvent("https://t.me/cats_channel/55")
        await message.handle_message(ev)
        await asyncio.sleep(0.05)
        assert list(config.active_tasks) == ["msg_cats_channel_55"]        # visible to /jobs and /cancel
        assert calls == [("cats_channel", 55)]

        dup = FakeEvent("https://t.me/cats_channel/55")
        await message.handle_message(dup)                                   # same link again -> refused
        assert any("already being cloned" in t for t in dup.texts) and len(calls) == 1

        cancel = CancelEvent("msg_cats_channel_55")
        await system.handle_cancel(cancel)
        await drain()
        assert config.active_tasks == {}
        assert any("Cancelled" in t for t in ev.texts)

    asyncio.run(run())


def test_unexpected_error_is_reported_to_user(monkeypatch):
    async def boom(chat, msg_id, status_cb):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(message.worker, "clone_single_message", boom)

    async def run():
        ev = FakeEvent("https://t.me/durov/1")
        await message.handle_message(ev)
        await drain()
        assert any("failed" in t and "kaboom" in t for t in ev.texts)
        assert config.active_tasks == {}

    asyncio.run(run())


# ---------- URL dedup ----------

def test_links_are_never_refused_as_duplicates(monkeypatch):
    seen = []

    async def fake_x(url, status_cb):
        seen.append(url)
        return True

    monkeypatch.setattr(message.worker, "clone_x_media", fake_x)

    async def run():
        texts = []
        for text in ["https://x.com/a/status/1", "https://twitter.com/a/status/1?s=20",
                     "https://x.com/a/status/1/photo/1", "force https://x.com/a/status/1"]:
            ev = FakeEvent(text)
            await message.handle_message(ev)
            await drain()
            texts += ev.texts
        return texts

    texts = asyncio.run(run())
    assert len(seen) == 4                                            # every one of them was downloaded
    assert not any("Already sent" in t for t in texts)
    assert db.get_sent_urls() == set()                               # nothing is remembered for X


def test_instagram_posts_are_remembered_only_when_they_succeed(monkeypatch):
    results, seen = [False], []

    async def fake_ig(url, status_cb):
        seen.append(url)
        return results[0]

    monkeypatch.setattr(message.worker, "clone_ig_media", fake_ig)

    async def send(text):
        ev = FakeEvent(text)
        await message.handle_message(ev)
        await drain()
        return ev

    async def run():
        await send("https://www.instagram.com/p/CxAb/")
        assert db.get_sent_urls() == set()                           # failed: not remembered
        results[0] = True
        await send("https://www.instagram.com/p/CxAb/")
        assert db.get_sent_urls() == {"https://instagram.com/p/CxAb"}

    asyncio.run(run())
    assert len(seen) == 2


def test_multiline_share_with_extra_text_reaches_the_right_url(monkeypatch):
    seen = []

    async def fake_ig(url, status_cb):
        seen.append(url)
        return True

    monkeypatch.setattr(message.worker, "clone_ig_media", fake_ig)

    async def run():
        await message.handle_message(FakeEvent("so cute \u2615\nhttps://www.instagram.com/reel/XYZ/?igsh=1\nthanks"))
        await drain()

    asyncio.run(run())
    assert seen == ["https://www.instagram.com/reel/XYZ/?igsh=1"]


# ---------- pending jobs: resume after restart, not after /cancel or completion ----------

def _pending():
    return json.loads(db.JOBS_DB_PATH.read_text()) if db.JOBS_DB_PATH.exists() else []


def test_shutdown_keeps_pending_job_and_resume_uses_typed_source(monkeypatch):
    sources = []

    async def clone(source, status_cb):
        sources.append(source)
        await asyncio.sleep(60)

    monkeypatch.setattr(jobs.worker, "clone_group", clone)

    async def run():
        await message.handle_message(FakeEvent("https://t.me/c/1234567"))
        await asyncio.sleep(0.05)
        task = list(config.active_tasks.values())[0]
        task.cancel()                                        # what asyncio.run() does on Ctrl+C / SIGTERM
        await asyncio.gather(task, return_exceptions=True)
        assert [j["source"] for j in _pending()] == ["https://t.me/c/1234567"]   # survives shutdown

        # "restart"
        sources.clear()
        n = jobs.resume_pending_jobs(lambda key: (lambda text: asyncio.sleep(0)))
        await asyncio.sleep(0.05)
        assert n == 1 and sources == [-1001234567]           # an int ID, not the text of the link
        for t in list(config.active_tasks.values()):
            t.cancel()
        await drain()

    asyncio.run(run())


def test_cancel_and_completion_remove_pending_job(monkeypatch):
    async def forever(source, status_cb):
        await asyncio.sleep(60)

    async def quick(source, status_cb):
        return None

    async def run():
        monkeypatch.setattr(jobs.worker, "clone_group", forever)
        await message.handle_message(FakeEvent("durov_channel"))
        await asyncio.sleep(0.05)
        assert len(_pending()) == 1
        await system.handle_cancel(CancelEvent())            # explicit /cancel (all)
        await drain()
        assert _pending() == []

        monkeypatch.setattr(jobs.worker, "clone_group", quick)
        await message.handle_message(FakeEvent("another_channel"))
        await drain()
        assert _pending() == []                              # finished normally

    asyncio.run(run())


def test_failed_job_is_not_retried_forever(monkeypatch):
    async def boom(source, status_cb):
        raise RuntimeError("nope")

    monkeypatch.setattr(jobs.worker, "clone_group", boom)

    async def run():
        ev = FakeEvent("durov_channel")
        await message.handle_message(ev)
        await drain()
        assert any("failed" in t for t in ev.texts)
        assert _pending() == []

    asyncio.run(run())


def test_resume_handles_legacy_and_unreadable_entries(monkeypatch):
    sources = []

    async def clone(source, status_cb):
        sources.append(source)

    monkeypatch.setattr(jobs.worker, "clone_group", clone)
    db.JOBS_DB_PATH.write_text(json.dumps([{"source": "-1001234"}, {"source": "durov"}, {"source": "!!"}]))

    async def run():
        n = jobs.resume_pending_jobs(lambda key: (lambda text: asyncio.sleep(0)))
        await drain()
        return n

    assert asyncio.run(run()) == 2
    assert sorted(map(str, sources)) == ["-1001234", "durov"]
    assert sources.count(-1001234) == 1                      # legacy numeric string became an int
    assert "!!" not in [j["source"] for j in _pending()]     # unreadable entry dropped


# ---------- Instagram links through the real handler ----------

@pytest.mark.parametrize("sent,expected_url", [
    ("https://www.instagram.com/reels/CxAb/", "https://www.instagram.com/reels/CxAb/"),
    ("https://www.instagram.com/someuser/reel/CxAb/?igsh=1", "https://www.instagram.com/someuser/reel/CxAb/?igsh=1"),
    ("https://www.instagram.com/share/reel/BAbCd/", "https://www.instagram.com/share/reel/BAbCd/"),
    ("https://instagram.com/p/CxAb/", "https://www.instagram.com/p/CxAb/"),        # host normalised for gallery-dl
    ("https://m.instagram.com/p/CxAb/", "https://www.instagram.com/p/CxAb/"),
    ("http://instagr.am/p/CxAb/", "https://www.instagram.com/p/CxAb/"),
    ("look at this\nhttps://www.instagram.com/reel/CxAb/\nwow", "https://www.instagram.com/reel/CxAb/"),
])
def test_instagram_links_are_downloaded(monkeypatch, sent, expected_url):
    seen = []

    async def fake_ig(url, status_cb):
        seen.append(url)
        return True

    monkeypatch.setattr(message.worker, "clone_ig_media", fake_ig)

    async def run():
        ev = FakeEvent(sent)
        await message.handle_message(ev)
        await drain()
        return ev

    ev = asyncio.run(run())
    assert seen == [expected_url]
    assert not any("not recognized" in t or "not supported" in t for t in ev.texts)


@pytest.mark.parametrize("sent", [
    "https://www.instagram.com/explore/tags/cats/",
    "https://www.instagram.com/accounts/login/",
    "https://www.instagram.com/",
])
def test_unsupported_instagram_links_get_a_clear_message(monkeypatch, sent):
    called = []

    async def fake_ig(url, status_cb):
        called.append(url)
        return True

    monkeypatch.setattr(message.worker, "clone_ig_media", fake_ig)

    async def run():
        ev = FakeEvent(sent)
        await message.handle_message(ev)
        await drain()
        return ev

    ev = asyncio.run(run())
    assert called == []
    assert any("Instagram link not supported" in t and "post, reel, story or profile" in t for t in ev.texts)
    assert not any("not recognized" in t for t in ev.texts)         # the old, confusing message


def test_a_post_sent_again_in_another_link_style_is_sent_again(monkeypatch):
    seen = []

    async def fake_ig(url, status_cb):
        seen.append(url)
        return True

    monkeypatch.setattr(message.worker, "clone_ig_media", fake_ig)

    async def send(text):
        ev = FakeEvent(text)
        await message.handle_message(ev)
        await drain()
        return ev

    async def run():
        first = await send("https://www.instagram.com/p/CxAb/")
        second = await send("https://www.instagram.com/reels/CxAb/?igsh=9")
        third = await send("force https://www.instagram.com/reel/CxAb/")
        return first.texts + second.texts + third.texts

    texts = asyncio.run(run())
    assert len(seen) == 3 and not any("Already sent" in t for t in texts)
    assert db.get_sent_urls() == {"https://instagram.com/p/CxAb"}    # one entry for the post, for profile runs
