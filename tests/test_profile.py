import asyncio
import importlib.util
import shutil
import sys

import pytest

import config
import db
from handlers import message
from worker import gallery, media_sites

from test_jobs import FakeEvent, drain  # same fake Telegram event as the handler tests

CODES = ["AAA", "BBB", "CCC", "DDD", "EEE"]          # newest first, like Instagram lists them


def post_line(code, style="p"):
    return f"https://www.instagram.com/{style}/{code}/"


@pytest.fixture
def env(monkeypatch, tmp_path):
    config.active_tasks.clear()
    monkeypatch.setattr(db, "URLS_DB_PATH", tmp_path / "sent_urls.json")
    monkeypatch.setattr(db, "_sent_urls", set())
    monkeypatch.setattr(db, "JOBS_DB_PATH", tmp_path / "pending_jobs.json")
    monkeypatch.setattr(media_sites, "PROFILE_POST_DELAY", 0)
    monkeypatch.setattr(media_sites, "PROFILE_MAX_POSTS", 20)
    monkeypatch.setattr(media_sites, "_cookie_args", lambda site: ["--cookies", "c.txt"])
    yield tmp_path
    config.active_tasks.clear()


class Profile:
    """Stands in for gallery-dl's listing and for downloading a single post."""

    def __init__(self, monkeypatch, lines, ok=True, err="", fail=()):
        self.lines, self.ok, self.err, self.fail = lines, ok, err, set(fail)
        self.listed_until = None        # the last line gallery-dl was asked to print
        self.downloaded = []
        self.statuses = []
        monkeypatch.setattr(media_sites, "stream_gallery_dl_print", self.stream)
        monkeypatch.setattr(media_sites, "clone_ig_media", self.clone_post)

    async def stream(self, url, extra_args, fmt, on_line, timeout=0):
        self.url, self.fmt = url, fmt
        for line in self.lines:
            self.listed_until = line
            if on_line(line):
                return True, ""
        return self.ok, self.err

    async def clone_post(self, url, status_cb):
        self.downloaded.append(url)
        await status_cb("🔍 Instagram link detected")
        if any(code in url for code in self.fail):
            await status_cb("❌ Instagram download failed:\n`boom`")
            return False
        return True

    async def status(self, text):
        self.statuses.append(text)


def run(coro):
    return asyncio.run(coro)


# ---------- listing + limit + resume ----------

def test_takes_the_newest_posts_up_to_the_limit_and_stops_listing_early(env, monkeypatch):
    monkeypatch.setattr(media_sites, "PROFILE_MAX_POSTS", 2)
    # every post appears once per picture, like gallery-dl's per-file output
    lines = [post_line(c) for c in CODES for _ in range(3)]
    prof = Profile(monkeypatch, lines)
    assert run(media_sites.clone_ig_profile("anna", prof.status)) is True
    assert prof.downloaded == [post_line("AAA"), post_line("BBB")]
    assert prof.listed_until == post_line("BBB") and lines.index(prof.listed_until) < 6   # didn't read the rest
    assert prof.url == "https://www.instagram.com/anna/" and prof.fmt == "{post_url}"
    assert "2 posts sent, 0 failed" in prof.statuses[-1]
    assert "send the profile link again" in prof.statuses[-1]               # more may exist


def test_sending_the_link_again_continues_with_older_posts(env, monkeypatch):
    monkeypatch.setattr(media_sites, "PROFILE_MAX_POSTS", 2)
    prof = Profile(monkeypatch, [post_line(c) for c in CODES])
    run(media_sites.clone_ig_profile("anna", prof.status))
    run(media_sites.clone_ig_profile("anna", prof.status))
    run(media_sites.clone_ig_profile("anna", prof.status))
    assert [u.split("/")[-2] for u in prof.downloaded] == CODES               # each exactly once, no repeats
    assert "1 posts sent" in prof.statuses[-1] and "send the profile link again" not in prof.statuses[-1]
    run(media_sites.clone_ig_profile("anna", prof.status))
    assert len(prof.downloaded) == 5 and "nothing new" in prof.statuses[-1]


def test_posts_sent_earlier_by_link_are_skipped_whatever_the_link_style(env, monkeypatch):
    run(db.mark_url_sent("https://instagram.com/p/BBB"))                      # sent before as /reel/BBB or /p/BBB
    prof = Profile(monkeypatch, [post_line("AAA"), post_line("BBB", "reel"), post_line("CCC")])
    run(media_sites.clone_ig_profile("anna", prof.status))
    assert [u.split("/")[-2] for u in prof.downloaded] == ["AAA", "CCC"]


def test_successful_posts_are_remembered_failed_ones_are_not(env, monkeypatch):
    prof = Profile(monkeypatch, [post_line(c) for c in CODES[:3]], fail=["BBB"])
    assert run(media_sites.clone_ig_profile("anna", prof.status)) is True
    assert db.get_sent_urls() == {"https://instagram.com/p/AAA", "https://instagram.com/p/CCC"}
    assert "2 posts sent, 1 failed" in prof.statuses[-1]
    prof.downloaded.clear()
    run(media_sites.clone_ig_profile("anna", prof.status))                    # the failed one is retried
    assert prof.downloaded == [post_line("BBB")]


def test_force_sends_everything_again(env, monkeypatch):
    run(db.mark_url_sent("https://instagram.com/p/AAA"))
    prof = Profile(monkeypatch, [post_line("AAA"), post_line("BBB")])
    run(media_sites.clone_ig_profile("anna", prof.status, force=True))
    assert len(prof.downloaded) == 2


def test_stories_and_other_lines_are_not_posts(env, monkeypatch):
    lines = ["https://www.instagram.com/stories/anna/123/", "None", "", post_line("AAA", "reels")]
    prof = Profile(monkeypatch, lines)
    run(media_sites.clone_ig_profile("anna", prof.status))
    assert prof.downloaded == [post_line("AAA")]


# ---------- failure handling ----------

def test_three_failures_in_a_row_stop_the_run(env, monkeypatch):
    prof = Profile(monkeypatch, [post_line(c) for c in CODES], fail=CODES)
    assert run(media_sites.clone_ig_profile("anna", prof.status)) is False
    assert len(prof.downloaded) == 3                                          # not all 5: probably expired cookies
    assert "Stopped after 3 failed posts in a row" in prof.statuses[-1] and "boom" in prof.statuses[-1]


def test_listing_error_is_reported(env, monkeypatch):
    prof = Profile(monkeypatch, [], ok=False, err="[instagram][error] HttpError: '404 Not Found'")
    assert run(media_sites.clone_ig_profile("nobody", prof.status)) is False
    assert "Could not list the posts of @nobody" in prof.statuses[-1] and "404" in prof.statuses[-1]


def test_a_listing_that_breaks_midway_still_sends_what_it_found(env, monkeypatch):
    prof = Profile(monkeypatch, [post_line("AAA"), post_line("BBB")], ok=False, err="rate limited")
    assert run(media_sites.clone_ig_profile("anna", prof.status)) is True
    assert len(prof.downloaded) == 2 and "more posts" in prof.statuses[-1]


def test_cookies_are_required(env, monkeypatch):
    monkeypatch.setattr(media_sites, "_cookie_args", lambda site: [])
    prof = Profile(monkeypatch, [post_line("AAA")])
    assert run(media_sites.clone_ig_profile("anna", prof.status)) is False
    assert prof.downloaded == [] and "cookies_ig.txt" in prof.statuses[-1]


# ---------- through the real handler ----------

def test_profile_link_starts_a_profile_job_and_is_never_refused_as_duplicate(env, monkeypatch):
    calls = []

    async def fake_profile(username, status_cb, force=False):
        calls.append((username, force))
        return True

    monkeypatch.setattr(message.worker, "clone_ig_profile", fake_profile)

    async def send(text):
        ev = FakeEvent(text)
        await message.handle_message(ev)
        await drain()
        return ev

    async def go():
        await send("https://www.instagram.com/Anna/")
        ev = await send("https://www.instagram.com/anna/?hl=en")             # again: continues, no "Already sent"
        assert not any("Already sent" in t for t in ev.texts)
        await send("force https://www.instagram.com/anna/")
        await send("look\nhttps://instagram.com/bob")                          # text around it, no www

    run(go())
    assert calls == [("Anna", False), ("anna", False), ("anna", True), ("bob", False)]
    assert db.get_sent_urls() == set()                                         # the profile itself is never marked as sent


def test_two_runs_for_the_same_profile_cannot_overlap(env, monkeypatch):
    started = []

    async def slow(username, status_cb, force=False):
        started.append(username)
        await asyncio.sleep(0.3)
        return True

    monkeypatch.setattr(message.worker, "clone_ig_profile", slow)

    async def go():
        await message.handle_message(FakeEvent("https://www.instagram.com/anna/"))
        await asyncio.sleep(0.05)
        ev = FakeEvent("https://www.instagram.com/ANNA/")
        await message.handle_message(ev)
        assert any("already being downloaded" in t for t in ev.texts)
        await drain()

    run(go())
    assert started == ["anna"]


# ---------- the streaming subprocess helper ----------

def fake_gallery_dl(monkeypatch, script):
    procs = []
    real = asyncio.create_subprocess_exec

    async def spy(*args, **kw):
        p = await real(*args, **kw)
        procs.append(p)
        return p

    monkeypatch.setattr(gallery.asyncio, "create_subprocess_exec", spy)
    monkeypatch.setattr(gallery, "_gallery_dl_cmd", lambda: [sys.executable, "-c", script])
    return procs


def test_stream_stops_and_kills_gallery_dl_as_soon_as_enough_lines_arrived(monkeypatch):
    procs = fake_gallery_dl(monkeypatch, "import sys,time\nfor i in range(100):\n print('line',i,flush=True)\n time.sleep(0.2)\n")
    got = []

    def on_line(line):
        got.append(line)
        return len(got) >= 3

    ok, err = run(asyncio.wait_for(gallery.stream_gallery_dl_print("u", [], "{x}", on_line), 10))
    assert ok and got == ["line 0", "line 1", "line 2"]
    assert procs[0].returncode is not None                                     # not left running for 20 seconds


def test_stream_delivers_everything_when_the_process_finishes(monkeypatch):
    fake_gallery_dl(monkeypatch, "print('a'); print('b')")
    got = []
    assert run(gallery.stream_gallery_dl_print("u", [], "{x}", lambda l: got.append(l) or False)) == (True, "")
    assert got == ["a", "b"]


def test_stream_reports_gallery_dl_errors(monkeypatch):
    fake_gallery_dl(monkeypatch, "import sys; sys.stderr.write('[instagram][error] login required'); sys.exit(4)")
    ok, err = run(gallery.stream_gallery_dl_print("u", [], "{x}", lambda l: False))
    assert not ok and "login required" in err


def test_stream_times_out_and_cleans_up(monkeypatch):
    procs = fake_gallery_dl(monkeypatch, "import time; time.sleep(30)")
    ok, err = run(gallery.stream_gallery_dl_print("u", [], "{x}", lambda l: False, timeout=0.4))
    assert not ok and "timeout" in err and procs[0].returncode is not None


def test_stream_is_killed_when_the_job_is_cancelled(monkeypatch):
    procs = fake_gallery_dl(monkeypatch, "import time; time.sleep(30)")

    async def go():
        t = asyncio.create_task(gallery.stream_gallery_dl_print("u", [], "{x}", lambda l: False))
        await asyncio.sleep(0.4)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t

    run(go())
    assert procs[0].returncode is not None


def test_stream_asks_for_unbuffered_output(monkeypatch):
    fake_gallery_dl(monkeypatch, "import os; print(os.environ.get('PYTHONUNBUFFERED'))")
    got = []
    run(gallery.stream_gallery_dl_print("u", [], "{x}", lambda l: got.append(l) or False))
    assert got == ["1"]


@pytest.mark.skipif(importlib.util.find_spec("gallery_dl") is None and shutil.which("gallery-dl") is None,
                    reason="gallery-dl is not installed")
def test_real_gallery_dl_print_mode_lists_without_downloading(tmp_path, monkeypatch):
    import functools
    import http.server
    import threading
    served = tmp_path / "s"
    served.mkdir()
    (served / "a.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"x" * 3000 + b"\xff\xd9")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(served))
    handler.log_message = lambda *a, **k: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.chdir(tmp_path)          # gallery-dl would drop downloads into the current directory
        got = []
        ok, err = run(gallery.stream_gallery_dl_print(f"http://127.0.0.1:{server.server_address[1]}/a.jpg", [], "{filename}", lambda l: got.append(l) or False))
        assert ok, err
        assert got == ["a"]
        assert not [p for p in tmp_path.rglob("*") if p.is_file() and p.parent != served]   # nothing downloaded
    finally:
        server.shutdown()


# ---------- regressions found by running the suite on Windows ----------

def test_stream_waits_for_the_process_to_exit_after_its_output_ends(monkeypatch):
    # stdout is closed first and the process exits a moment later (normal for gallery-dl, especially on
    # Windows). The old code killed it at that point, which turned a successful run into a failure.
    fake_gallery_dl(monkeypatch, "import os, time\nprint('a', flush=True)\nos.close(1)\ntime.sleep(0.6)\n")
    got = []
    assert run(gallery.stream_gallery_dl_print("u", [], "{x}", lambda l: got.append(l) or False)) == (True, "")
    assert got == ["a"]


def test_failure_without_any_error_text_still_says_what_happened(monkeypatch):
    fake_gallery_dl(monkeypatch, "import sys; sys.exit(3)")
    ok, err = run(gallery.stream_gallery_dl_print("u", [], "{x}", lambda l: False))
    assert not ok and "exited with code 3" in err
    ok, err = run(gallery._run_gallery_dl("u", ".", []))
    assert not ok and "exited with code 3" in err


def test_gallery_dl_is_started_through_the_current_python_when_installed():
    import importlib.util
    if importlib.util.find_spec("gallery_dl") is None:
        pytest.skip("gallery-dl is not installed in this Python")
    assert gallery._gallery_dl_cmd() == [sys.executable, "-m", "gallery_dl"]    # no launcher .exe in between
