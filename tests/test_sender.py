import asyncio
import types

import pytest

from worker.sender import CAPTION_LIMIT, MESSAGE_LIMIT, pick_caption, send_media, split_text


def msgs(*texts):
    return [types.SimpleNamespace(text=t) for t in texts]


def test_pick_caption_uses_whichever_album_message_has_the_text():
    assert pick_caption(msgs("hello", "", "")) == "hello"      # usual case: caption on the first item
    assert pick_caption(msgs("", "", "tail")) == "tail"
    assert pick_caption(msgs("", None, "")) == ""


def test_split_text_respects_limit_and_loses_nothing():
    para = "x" * 1000
    text = "\n".join([para] * 9)                              # 9009 chars
    chunks = split_text(text)
    assert all(len(c) <= MESSAGE_LIMIT for c in chunks) and len(chunks) == 3
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")
    assert split_text("") == [] and split_text("short") == ["short"]
    hard = split_text("y" * 5000)                              # no line breaks at all
    assert [len(c) for c in hard] == [4096, 904]


class Recorder:
    def __init__(self):
        self.calls = []

    async def send_file(self, dest, file=None, caption=None, reply_to=None, parse_mode=None):
        self.calls.append(("file", file, caption))

    async def send_message(self, dest, text, reply_to=None, parse_mode=None):
        self.calls.append(("msg", text))


def test_send_media_single_album_and_long_caption():
    r = Recorder()
    asyncio.run(send_media(r, "d", ["a.jpg"], "cap", 7))
    asyncio.run(send_media(r, "d", ["a.jpg", "b.jpg"], "cap", 7))
    assert r.calls == [("file", "a.jpg", "cap"), ("file", ["a.jpg", "b.jpg"], ["cap", ""])]

    r = Recorder()
    long_caption = "L" * (CAPTION_LIMIT + 500)
    asyncio.run(send_media(r, "d", ["a.jpg", "b.jpg"], long_caption, 7))
    assert r.calls[0] == ("file", ["a.jpg", "b.jpg"], ["", ""])      # media goes out without the oversized caption
    assert r.calls[1] == ("msg", long_caption)                        # ...and the full text follows

    r = Recorder()
    asyncio.run(send_media(r, "d", [], "cap", 7))
    assert r.calls == []
