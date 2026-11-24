import pytest

from parsing import (IG_POST_RE, MESSAGE_PATTERN, canonical_instagram, extract_target, instagram_profile,
                     is_instagram, normalize_url, parse_source, parse_tg_link, wants_force)


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
    text = "caf\u00e9 \u2615\nhttps://x.com/a/status/1"
    m = MESSAGE_PATTERN.match(text)
    assert m and m.group(1) == text


def test_commands_are_not_handled_as_links():
    assert MESSAGE_PATTERN.match("/ping") is None


def test_telethon_accepts_compiled_pattern():
    from telethon import events
    builder = events.NewMessage(pattern=MESSAGE_PATTERN)
    assert builder.pattern("a\nb") is not None


@pytest.mark.parametrize("text,expected", [
    ("look at this\nhttps://x.com/a/status/1 so good", "https://x.com/a/status/1"),
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


@pytest.mark.parametrize("a,b", [
    ("https://twitter.com/user/status/123?s=20&t=abc", "https://x.com/user/status/123"),
    ("https://x.com/user/status/123/photo/1", "https://x.com/user/status/123"),
    ("https://www.instagram.com/p/AAA/?igsh=xyz", "https://instagram.com/p/AAA"),
    ("https://www.tiktok.com/@u/video/1?lang=en#top", "https://tiktok.com/@u/video/1"),
])
def test_normalize_url(a, b):
    assert normalize_url(a) == b


def test_normalize_url_keeps_different_posts_apart():
    assert normalize_url("https://x.com/user/status/123") != normalize_url("https://x.com/user/status/124")
    assert normalize_url("https://instagram.com/p/AAA") != normalize_url("https://instagram.com/p/BBB")


@pytest.mark.parametrize("text,expected", [
    ("force https://x.com/a/status/1", True),
    ("https://x.com/a/status/1 FORCE", True),
    ("https://x.com/a/status/1", False),
    ("enforce https://x.com/a/status/1", False),
])
def test_wants_force(text, expected):
    assert wants_force(text) is expected


# ---------- Instagram link shapes ----------

IG_SHAPES = [
    ("https://www.instagram.com/p/CxAbCdEfGhI/", "CxAbCdEfGhI"),
    ("https://www.instagram.com/reel/CxAbCdEfGhI/", "CxAbCdEfGhI"),
    ("https://www.instagram.com/reels/CxAbCdEfGhI/", "CxAbCdEfGhI"),               # plural
    ("https://www.instagram.com/tv/CxAbCdEfGhI/", "CxAbCdEfGhI"),
    ("https://www.instagram.com/someuser/reel/CxAbCdEfGhI/", "CxAbCdEfGhI"),       # newer links carry the username
    ("https://www.instagram.com/someuser/p/CxAbCdEfGhI/", "CxAbCdEfGhI"),
    ("https://www.instagram.com/share/reel/BAbCdEfGhI/", "BAbCdEfGhI"),             # "share" links
    ("https://www.instagram.com/share/p/BAbCdEfGhI/", "BAbCdEfGhI"),
    ("https://www.instagram.com/share/BAbCdEfGhI", "BAbCdEfGhI"),
    ("https://www.instagram.com/stories/someuser/3123456789012345678/", "someuser"),
    ("https://www.instagram.com/stories/highlights/17900000000000000/", "highlights"),
    ("https://instagram.com/reel/CxAbCdEfGhI/?igsh=MWJ4eXo", "CxAbCdEfGhI"),
]


@pytest.mark.parametrize("url,code", IG_SHAPES)
def test_instagram_post_links_are_recognised(url, code):
    m = IG_POST_RE.search(url)
    assert m and m.group(1) == code


@pytest.mark.parametrize("url", [
    "https://www.instagram.com/someuser/",          # profile
    "https://www.instagram.com/someuser",
    "https://www.instagram.com/",
    "https://www.instagram.com/explore/tags/cats/",
    "https://www.instagram.com/share/",
    "https://example.com/p/CxAbCdEfGhI/",
])
def test_other_instagram_urls_are_not_posts(url):
    assert IG_POST_RE.search(url) is None


@pytest.mark.parametrize("url,expected", [
    ("https://m.instagram.com/p/CxAb/", "https://www.instagram.com/p/CxAb/"),
    ("https://instagram.com/p/CxAb/", "https://www.instagram.com/p/CxAb/"),
    ("http://instagr.am/p/CxAb/", "https://www.instagram.com/p/CxAb/"),
    ("https://www.instagram.com/p/CxAb/", "https://www.instagram.com/p/CxAb/"),
])
def test_canonical_instagram(url, expected):
    assert is_instagram(url) and canonical_instagram(url) == expected


def test_is_instagram_does_not_match_lookalikes():
    assert not is_instagram("https://notinstagram.com/p/x/")
    assert not is_instagram("https://instagram.com.evil.example/p/x/")
    assert not is_instagram("https://x.com/a/status/1")


def test_one_instagram_post_has_one_dedup_key_whatever_the_link_style():
    keys = {normalize_url(u) for u in [
        "https://www.instagram.com/p/CxAb/?igsh=1",
        "https://www.instagram.com/reel/CxAb/",
        "https://www.instagram.com/reels/CxAb/",
        "https://www.instagram.com/someuser/reel/CxAb/",
        "https://instagr.am/p/CxAb",
    ]}
    assert keys == {"https://instagram.com/p/CxAb"}
    assert normalize_url("https://www.instagram.com/p/CxAb/") != normalize_url("https://www.instagram.com/p/CxAc/")
    assert normalize_url("https://www.instagram.com/stories/u/1/") == "https://instagram.com/stories/u/1"


# ---------- Instagram profile links ----------

@pytest.mark.parametrize("url,user", [
    ("https://www.instagram.com/someuser/", "someuser"),
    ("https://www.instagram.com/someuser", "someuser"),
    ("https://www.instagram.com/some.user_1/?hl=en", "some.user_1"),
    ("https://instagram.com/SomeUser/#top", "SomeUser"),
])
def test_instagram_profile_links(url, user):
    assert instagram_profile(url) == user


@pytest.mark.parametrize("url", [
    "https://www.instagram.com/",
    "https://www.instagram.com/p/CxAb/",                  # a post
    "https://www.instagram.com/someuser/reel/CxAb/",      # a post
    "https://www.instagram.com/explore/",                 # reserved names are not users
    "https://www.instagram.com/accounts/login/",
    "https://www.instagram.com/reels/",
    "https://www.instagram.com/stories/someuser/123/",
    "https://example.com/someuser/",
    "https://www.instagram.com/" + "a" * 31 + "/",         # longer than a user name can be
])
def test_not_an_instagram_profile(url):
    assert instagram_profile(url) is None
