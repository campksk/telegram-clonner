import pytest

from parsing import MESSAGE_PATTERN, extract_target, parse_source, parse_tg_link


@pytest.mark.parametrize("url,expected", [
    ("https://t.me/durov/123", ("durov", 123)),
    ("https://t.me/cats_channel/55", ("cats_channel", 55)),      # username starting with "c"
    ("https://t.me/c/1234567/89", (-1001234567, 89)),
    ("https://t.me/c/1234567/10/89", (-1001234567, 89)),         # forum topic link: last number is the message
    ("https://t.me/somechan/10/89", ("somechan", 89)),
    ("https://t.me/durov/123?single", ("durov", 123)),
    ("https://t.me/durov", None),
    ("https://t.me/+AbCdEf", None),
    ("https://t.me/c/1234567", None),
    ("https://example.com/durov/123", None),
])
def test_parse_tg_link(url, expected):
    assert parse_tg_link(url) == expected


def test_multiline_message_matches():
    text = "ดูอันนี้\nhttps://x.com/a/status/1"
    m = MESSAGE_PATTERN.match(text)
    assert m and m.group(1) == text


def test_commands_are_not_handled_as_links():
    assert MESSAGE_PATTERN.match("/ping") is None


def test_telethon_accepts_compiled_pattern():
    from telethon import events
    builder = events.NewMessage(pattern=MESSAGE_PATTERN)
    assert builder.pattern("a\nb") is not None


@pytest.mark.parametrize("text,expected", [
    ("ดูอันนี้\nhttps://x.com/a/status/1 เจ๋งมาก", "https://x.com/a/status/1"),
    ("(https://www.tiktok.com/@u/video/1).", "https://www.tiktok.com/@u/video/1"),
    ("durov", "durov"),
    ("  123456  ", "123456"),
    ("hello world", "hello world"),      # not a URL and not a single token -> stays unrecognised
    ("", ""),
])
def test_extract_target(text, expected):
    assert extract_target(text) == expected


@pytest.mark.parametrize("target,expected", [
    ("https://t.me/durov", "durov"),
    ("https://t.me/durov/", "durov"),
    ("https://t.me/durov?foo=1", "durov"),
    ("https://t.me/c/1234567", -1001234567),
    ("-1001234567", -1001234567),
    ("durov", "durov"),
    ("abc", None),
    ("hello world", None),
    ("https://t.me/+AbCdEf", None),
    ("https://t.me/joinchat/AbCdEf", None),
])
def test_parse_source(target, expected):
    assert parse_source(target) == expected
