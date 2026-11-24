import asyncio
import functools
import http.server
import importlib.util
import json
import os
import shutil
import threading
from pathlib import Path

import pytest

from worker import gallery, media_sites
from worker.gallery import album_batches, collect_posts

BIG = b"x" * 2048


def touch(root, rel, data=BIG):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


# ---------- collect_posts ----------

def test_one_post_per_directory_with_username_and_oldest_first(tmp_path):
    for rel in ["tiktok/anna/300/300_01.jpeg", "tiktok/anna/300/300_02.jpeg", "tiktok/anna/200/200.mp4",
                "tiktok/bob/100/100_01.jpeg"]:
        touch(tmp_path, rel)
    posts = collect_posts(tmp_path)
    assert [(p.username, [os.path.basename(f) for f in p.files]) for p in posts] == [
        ("anna", ["200.mp4"]),                         # older post first (ids grow with time)
        ("anna", ["300_01.jpeg", "300_02.jpeg"]),      # files of a post in order
        ("bob", ["100_01.jpeg"]),
    ]


def test_shallower_layouts_are_tolerated(tmp_path):
    touch(tmp_path, "instagram/anna/1.jpg")            # no post directory: whole user dir is one post
    touch(tmp_path, "instagram/anna/2.jpg")
    touch(tmp_path, "twitter/3.jpg")                   # no username
    touch(tmp_path, "4.jpg")
    posts = {p.username: [os.path.basename(f) for f in p.files] for p in collect_posts(tmp_path)}
    assert posts == {"anna": ["1.jpg", "2.jpg"], None: ["3.jpg", "4.jpg"]}


def test_tiny_and_unknown_files_are_ignored(tmp_path):
    touch(tmp_path, "c/u/1/ok.jpg")
    touch(tmp_path, "c/u/1/tiny.jpg", data=b"x" * 10)
    touch(tmp_path, "c/u/1/info.json")
    touch(tmp_path, "c/u/1/notes.txt")
    assert [os.path.basename(f) for p in collect_posts(tmp_path) for f in p.files] == ["ok.jpg"]


def test_audio_track_is_dropped_from_a_picture_post_but_kept_when_it_is_all_there_is(tmp_path):
    touch(tmp_path, "tiktok/u/1/1_01.jpeg")
    touch(tmp_path, "tiktok/u/1/1_02.jpeg")
    touch(tmp_path, "tiktok/u/1/1 song.mp3")
    touch(tmp_path, "tiktok/u/2/2 only audio.mp3")
    posts = collect_posts(tmp_path)
    assert [os.path.basename(f) for f in posts[0].files] == ["1_01.jpeg", "1_02.jpeg"]
    assert [os.path.basename(f) for f in posts[1].files] == ["2 only audio.mp3"]


# ---------- album_batches ----------

def names(batches):
    return [[os.path.basename(f) for f in b] for b in batches]


def test_photos_and_videos_share_albums_and_albums_hold_at_most_10():
    files = [f"{i:02d}.jpg" for i in range(1, 12)] + ["v.mp4"]
    batches = names(album_batches(files))
    assert [len(b) for b in batches] == [10, 2] and batches[1] == ["11.jpg", "v.mp4"]


def test_other_file_types_are_never_albumed_with_photos():
    assert names(album_batches(["1.jpg", "2.webp", "3.jpg", "4.gif"])) == [["1.jpg", "3.jpg"], ["2.webp", "4.gif"]]
    assert names(album_batches(["a.webp", "b.webp"])) == [["a.webp", "b.webp"]]
    assert album_batches([]) == []


# ---------- end to end: the TikTok slideshow that used to arrive as single files ----------

def fake_runner(dirs, writer):
    async def run(url, job_dir, extra_args):
        dirs.append((job_dir, list(extra_args)))
        writer(job_dir)
        return True, ""
    return run


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(media_sites, "DOWNLOAD_DIR", tmp_path)
    monkeypatch.setattr(media_sites, "_cookie_args", lambda site: ["--cookies", "c.txt"])

    async def topic(name):
        return 5

    monkeypatch.setattr(media_sites, "get_or_create_topic", topic)
    from test_media_sites import FakeClient
    client = FakeClient()
    monkeypatch.setattr(gallery, "user_client", client)
    return client


async def noop(text):
    pass


def tiktok_slideshow(job_dir, images=3, ext=".jpeg", audio=True):
    for n in range(1, images + 1):
        touch(job_dir, f"tiktok/someuser/750001/750001_{n:02d} My title{ext}")
    if audio:
        touch(job_dir, "tiktok/someuser/750001/750001 My title 99.mp3")


def test_tiktok_slideshow_is_sent_as_one_album_without_the_music(env, monkeypatch):
    seen = []
    monkeypatch.setattr(media_sites, "_run_gallery_dl", fake_runner(seen, tiktok_slideshow))
    assert asyncio.run(media_sites.clone_tiktok_media("https://www.tiktok.com/@someuser/photo/750001", noop)) is True
    assert [os.path.basename(f) for f in env.sent] == [f"750001_{n:02d} My title.jpeg" for n in (1, 2, 3)]
    assert env.album_calls == 1 and env.single_calls == 0          # one album, not three single files
    assert "audio=false" in seen[0][1]                             # and the music isn't even downloaded


def test_webp_slideshow_is_still_one_group(env, monkeypatch):
    monkeypatch.setattr(media_sites, "_run_gallery_dl", fake_runner([], lambda d: tiktok_slideshow(d, ext=".webp", audio=False)))
    assert asyncio.run(media_sites.clone_tiktok_media("https://www.tiktok.com/@someuser/photo/750001", noop)) is True
    assert env.album_calls == 1 and env.single_calls == 0 and len(env.sent) == 3


def test_every_site_gets_a_directory_per_post(env, monkeypatch):
    seen = []
    monkeypatch.setattr(media_sites, "_run_gallery_dl", fake_runner(seen, lambda d: touch(d, "c/u/1/a.jpg")))
    asyncio.run(media_sites.clone_x_media("https://x.com/a/status/1", noop))
    asyncio.run(media_sites.clone_ig_media("https://www.instagram.com/p/A/", noop))
    asyncio.run(media_sites.clone_tiktok_media("https://www.tiktok.com/@a/video/1", noop))
    for _, args in seen:
        assert any(a.startswith("directory=") for a in args), args


@pytest.mark.parametrize("site,sample", [
    ("x", {"category": "twitter", "user": {"name": "anna"}, "tweet_id": 1}),
    ("ig", {"category": "instagram", "username": "anna", "post_id": 1}),
    ("tiktok", {"category": "tiktok", "user": "anna", "id": 1}),
])
def test_site_directory_templates_are_valid(site, sample):
    from gallery_dl import formatter
    args = gallery.site_args(site)
    template = json.loads([a for a in args if a.startswith("directory=")][0].split("=", 1)[1])
    assert [formatter.parse(part).format_map(sample) for part in template] == [sample["category"], "anna", "1"]


# ---------- the real gallery-dl, against a local web server (no internet needed) ----------

@pytest.mark.skipif(importlib.util.find_spec("gallery_dl") is None and shutil.which("gallery-dl") is None,
                    reason="gallery-dl is not installed")
def test_real_gallery_dl_puts_each_post_in_its_own_username_directory(tmp_path):
    served = tmp_path / "served"
    served.mkdir()
    (served / "a.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"x" * 3000 + b"\xff\xd9")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(served))
    handler.log_message = lambda *a, **k: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        out = tmp_path / "out"
        url = f"http://127.0.0.1:{server.server_address[1]}/a.jpg"
        args = ["-o", 'directory=["{category}","anna","98765"]']          # same mechanism SITE_ARGS uses
        ok, err = asyncio.run(gallery._run_gallery_dl(url, out, args))
        assert ok, err
        posts = collect_posts(out)
        assert [(p.username, len(p.files)) for p in posts] == [("anna", 1)]
        assert Path(posts[0].files[0]).parent.parts[-2:] == ("anna", "98765")      # (os.sep differs per platform)
    finally:
        server.shutdown()


# ---------- topic named after the poster's username ----------

@pytest.fixture
def topics_env(env, monkeypatch):
    """env + a get_or_create_topic that hands out ids and remembers which titles were asked for."""
    asked = {}

    async def topic(title):
        return asked.setdefault(title.lower(), 100 + len(asked))

    monkeypatch.setattr(media_sites, "get_or_create_topic", topic)
    return env, asked


@pytest.mark.parametrize("clone,url,fallback", [
    (media_sites.clone_tiktok_media, "https://www.tiktok.com/@someuser/photo/750001", "TikTok Downloads"),
    (media_sites.clone_ig_media, "https://www.instagram.com/p/AAA/", "Instagram Downloads"),
    (media_sites.clone_x_media, "https://x.com/someuser/status/1", "X Downloads"),
])
def test_every_site_posts_into_a_topic_named_after_the_username(topics_env, monkeypatch, clone, url, fallback):
    client, asked = topics_env
    statuses = []

    async def status(t):
        statuses.append(t)

    monkeypatch.setattr(media_sites, "_run_gallery_dl", fake_runner([], lambda d: touch(d, "c/someuser/1/a.jpg")))
    assert asyncio.run(clone(url, status)) is True
    assert list(asked) == ["someuser"]                    # not "TikTok Downloads" & co
    assert client.reply_tos == [100]
    assert any("Topic: **someuser**" in t for t in statuses)


def test_posts_of_different_users_go_to_their_own_topics_in_order(topics_env, monkeypatch):
    client, asked = topics_env

    def writer(d):
        touch(d, "tiktok/anna/1/1_01.jpeg")
        touch(d, "tiktok/anna/1/1_02.jpeg")
        touch(d, "tiktok/bob/2/2_01.jpeg")

    monkeypatch.setattr(media_sites, "_run_gallery_dl", fake_runner([], writer))
    assert asyncio.run(media_sites.clone_tiktok_media("https://www.tiktok.com/@anna", noop)) is True
    assert list(asked) == ["anna", "bob"]
    assert client.reply_tos == [100, 101]                 # anna's album, then bob's single file


def test_usernames_that_differ_only_by_case_share_one_topic(topics_env, monkeypatch):
    client, asked = topics_env
    monkeypatch.setattr(media_sites, "_run_gallery_dl",
                        fake_runner([], lambda d: (touch(d, "c/Anna/1/a.jpg"), touch(d, "c/anna/2/b.jpg"))))
    asyncio.run(media_sites.clone_ig_media("https://www.instagram.com/p/A/", noop))
    assert len(asked) == 1 and client.reply_tos == [100, 100]


def test_unknown_username_falls_back_to_the_site_topic(topics_env, monkeypatch):
    client, asked = topics_env
    monkeypatch.setattr(media_sites, "_run_gallery_dl", fake_runner([], lambda d: touch(d, "a.jpg")))
    assert asyncio.run(media_sites.clone_tiktok_media("https://www.tiktok.com/@x/video/1", noop)) is True
    assert list(asked) == ["tiktok downloads"]


def test_topic_title_is_cleaned_and_capped():
    assert media_sites._topic_title("  anna\n", "X") == "anna"
    assert media_sites._topic_title("a" * 300, "X") == "a" * 128
    assert media_sites._topic_title("", "Instagram Downloads") == "Instagram Downloads"
    assert media_sites._topic_title(None, "X Downloads") == "X Downloads"
