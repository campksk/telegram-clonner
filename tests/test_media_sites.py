import asyncio
import os
import sys
import types

import pytest

from worker import gallery, media_sites


class FakeClient:
    def __init__(self, fail_files=(), fail_albums=False):
        self.sent = []
        self.fail_files = set(fail_files)
        self.fail_albums = fail_albums
        self.album_calls = self.single_calls = 0
        self.reply_tos = []

    async def get_entity(self, ref):
        return types.SimpleNamespace(id=999)

    async def send_file(self, dest, file=None, caption=None, reply_to=None, parse_mode=None):
        self.reply_tos.append(reply_to)
        if isinstance(file, list):
            self.album_calls += 1
            if self.fail_albums:
                raise RuntimeError("album failed")
            visual = {os.path.splitext(f)[1].lower() in gallery.PHOTO_EXTS | gallery.VIDEO_EXTS for f in file}
            if len(visual) > 1:
                raise RuntimeError("MEDIA_INVALID: an album can't mix photos with other files")
            self.sent.extend(file)
        else:
            self.single_calls += 1
            if file in self.fail_files or "ALL" in self.fail_files:
                raise RuntimeError("file failed")
            self.sent.append(file)


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(media_sites, "DOWNLOAD_DIR", tmp_path)
    monkeypatch.setattr(media_sites, "_cookie_args", lambda site: ["--cookies", "c.txt"])

    async def topic(name):
        return 5

    monkeypatch.setattr(media_sites, "get_or_create_topic", topic)
    return tmp_path


def fake_gallery_dl(dirs, files_per_post=(2,)):
    async def run(url, job_dir, cookies):
        dirs.append(job_dir)
        for n, count in enumerate(files_per_post):
            d = job_dir / "site" / "someuser" / f"{1000 + n}"
            d.mkdir(parents=True)
            for k in range(count):
                (d / f"{k:03d}.jpg").write_bytes(b"x" * 2048)
        await asyncio.sleep(0.05)
        return True, ""
    return run


async def noop(text):
    pass


def test_concurrent_jobs_get_unique_dirs_and_are_cleaned_up(env, monkeypatch):
    dirs, statuses = [], []
    client = FakeClient()
    monkeypatch.setattr(gallery, "user_client", client)
    monkeypatch.setattr(media_sites, "_run_gallery_dl", fake_gallery_dl(dirs))

    async def status(t):
        statuses.append(t)

    async def run():
        # same second, and X URLs whose last path segment is identical ("1")
        return await asyncio.gather(
            media_sites.clone_x_media("https://x.com/a/status/10/photo/1", status),
            media_sites.clone_x_media("https://x.com/b/status/20/photo/1", status),
            media_sites.clone_ig_media("https://instagram.com/p/AAA/", status),
            media_sites.clone_tiktok_media("https://tiktok.com/@u/video/1", status),
        )

    assert asyncio.run(run()) == [True] * 4
    assert len({str(d) for d in dirs}) == 4
    assert list(env.iterdir()) == []                 # everything cleaned up
    assert len(client.sent) == 8                     # 4 jobs x 2 files, none deleted early
    assert any("Count: 2/2 files" in t for t in statuses)


def test_failure_paths_return_false_and_clean_up(env, monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(gallery, "user_client", client)

    async def failing(url, job_dir, cookies):
        return False, "boom"

    monkeypatch.setattr(media_sites, "_run_gallery_dl", failing)
    assert asyncio.run(media_sites.clone_x_media("https://x.com/a/status/1", noop)) is False
    assert list(env.iterdir()) == []

    async def empty(url, job_dir, cookies):
        return True, ""

    monkeypatch.setattr(media_sites, "_run_gallery_dl", empty)
    assert asyncio.run(media_sites.clone_tiktok_media("https://tiktok.com/@u/video/1", noop)) is False
    assert list(env.iterdir()) == []

    async def topic_fails(name):
        raise RuntimeError("no topics")

    monkeypatch.setattr(media_sites, "_run_gallery_dl", fake_gallery_dl([]))
    monkeypatch.setattr(media_sites, "get_or_create_topic", topic_fails)
    assert asyncio.run(media_sites.clone_ig_media("https://instagram.com/p/AAA/", noop)) is False
    assert list(env.iterdir()) == []                 # used to leak the download dir here
    assert client.sent == []


def test_instagram_without_cookies_fails_before_downloading(env, monkeypatch):
    monkeypatch.setattr(media_sites, "_cookie_args", lambda site: [])
    called = []

    async def run(url, job_dir, cookies):
        called.append(url)
        return True, ""

    monkeypatch.setattr(media_sites, "_run_gallery_dl", run)
    assert asyncio.run(media_sites.clone_ig_media("https://instagram.com/p/AAA/", noop)) is False
    assert called == [] and list(env.iterdir()) == []


def test_partial_and_total_upload_failure_are_reported_honestly(env, monkeypatch):
    statuses = []

    async def status(t):
        statuses.append(t)

    monkeypatch.setattr(media_sites, "_run_gallery_dl", fake_gallery_dl([], files_per_post=(2,)))

    # album fails -> falls back to one-by-one; one of the two files fails
    class OneBad(FakeClient):
        async def send_file(self, dest, file=None, **kw):
            if not isinstance(file, list) and file.endswith("001.jpg"):
                raise RuntimeError("bad file")
            return await super().send_file(dest, file=file, **kw)

    client = OneBad(fail_albums=True)
    monkeypatch.setattr(gallery, "user_client", client)
    assert asyncio.run(media_sites.clone_x_media("https://x.com/a/status/1", status)) is True
    assert any("Count: 1/2 files" in t for t in statuses)

    # everything fails -> False and an error (used to print "sent successfully")
    statuses.clear()
    monkeypatch.setattr(gallery, "user_client", FakeClient(fail_albums=True, fail_files={"ALL"}))
    assert asyncio.run(media_sites.clone_x_media("https://x.com/a/status/2", status)) is False
    assert any("Failed to upload any file" in t for t in statuses)
    assert not any("sent successfully" in t for t in statuses)


def test_gallery_dl_timeout_and_cancel_do_not_leave_a_process(monkeypatch, tmp_path):
    procs = []
    real = asyncio.create_subprocess_exec

    async def spy(*args, **kw):
        p = await real(*args, **kw)
        procs.append(p)
        return p

    monkeypatch.setattr(gallery.asyncio, "create_subprocess_exec", spy)

    monkeypatch.setattr(gallery, "_gallery_dl_cmd", lambda: [sys.executable, "-c", "import time; time.sleep(30)"])

    async def timeout_case():
        monkeypatch.setattr(gallery, "GALLERY_DL_TIMEOUT", 0.3)
        return await gallery._run_gallery_dl("http://x", tmp_path, [])

    ok, err = asyncio.run(timeout_case())
    assert not ok and "timeout" in err
    assert procs[-1].returncode is not None          # killed

    async def cancel_case():
        monkeypatch.setattr(gallery, "GALLERY_DL_TIMEOUT", 60)
        t = asyncio.create_task(gallery._run_gallery_dl("http://x", tmp_path, []))
        await asyncio.sleep(0.3)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        await asyncio.sleep(0.3)

    asyncio.run(cancel_case())
    assert procs[-1].returncode is not None          # killed on /cancel, no orphan
