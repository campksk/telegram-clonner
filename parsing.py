"""Pure text-parsing helpers (no Telegram/network imports, so they are easy to test)."""
import re
from urllib.parse import urlsplit

# Telethon matches this with re.match(); re.S lets the text span several lines.
MESSAGE_PATTERN = re.compile(r"^(?!/)(.+)$", re.S)

_URL_RE = re.compile(r"https?://\S+")

# t.me/<username>/<msg>, t.me/<username>/<topic>/<msg>,
# t.me/c/<chat_id>/<msg>, t.me/c/<chat_id>/<topic>/<msg>
_TG_LINK_RE = re.compile(
    r"https?://(?:www\.)?t\.me/"
    r"(?:c/(?P<chat_id>\d+)|(?P<username>[A-Za-z]\w{3,}))"
    r"/(?:\d+/)?(?P<msg_id>\d+)(?=[/?#]|$)"
)


# Instagram post-like links (everything gallery-dl can download as one item):
#   /p/<code>  /reel/<code>  /reels/<code>  /tv/<code>  /<user>/p|reel/<code>
#   /share/<code>  /share/p|reel/<code>  /stories/<user>/<id>  /stories/highlights/<id>
IG_POST_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/"
    r"(?:[^/?#]+/)?"
    r"(?:p|reels?|tv|stories|share)/"
    r"(?:(?:p|reels?|tv)/)?"
    r"([^/?#&]+)"
)
_IG_HOST_RE = re.compile(r"^https?://(?:(?:www|m)\.)?(?:instagram\.com|instagr\.am)(?=[/?#]|$)", re.I)


# First path segments of instagram.com that are not user names
_IG_RESERVED = {
    "p", "reel", "reels", "tv", "stories", "share", "explore", "accounts", "direct", "tags", "about", "legal",
    "developer", "web", "challenge", "locations", "topics", "emails", "session", "oauth", "directory",
    "privacy", "terms", "api", "graphql", "static", "ar", "nametag", "download", "lite", "fxcal",
}
_IG_PROFILE_RE = re.compile(r"^https?://(?:www\.)?instagram\.com/([A-Za-z0-9._]{1,30})/?(?:[?#].*)?$")


def instagram_profile(url: str) -> str | None:
    """The user name if `url` is a plain Instagram profile link (instagram.com/<user>/), else None."""
    m = _IG_PROFILE_RE.match(url)
    if not m or m.group(1).lower() in _IG_RESERVED:
        return None
    return m.group(1)


def is_instagram(url: str) -> bool:
    return bool(_IG_HOST_RE.match(url))


def canonical_instagram(url: str) -> str:
    """m.instagram.com / instagr.am / no-www -> https://www.instagram.com (what gallery-dl understands)."""
    return _IG_HOST_RE.sub("https://www.instagram.com", url, count=1)


def extract_target(text: str) -> str:
    """Return the first URL in `text` (trailing punctuation removed).

    If the text has no URL (a bare username / numeric ID) it is returned stripped, unchanged.
    """
    text = (text or "").strip()
    m = _URL_RE.search(text)
    return m.group(0).rstrip(".,;:!)]}>'\"") if m else text


def parse_tg_link(text: str):
    """Parse a link to one Telegram message -> (chat_ref, msg_id), or None."""
    m = _TG_LINK_RE.search(text)
    if not m:
        return None
    msg_id = int(m.group("msg_id"))
    if m.group("username"):
        return m.group("username"), msg_id
    return int("-100" + m.group("chat_id")), msg_id


def parse_source(target: str):
    """Parse a whole group/channel/user reference -> int | str, or None if unrecognised."""
    priv = re.search(r"t\.me/c/(\d+)", target)
    pub = re.search(r"t\.me/([^/?#]+)/?(?:[?#].*)?$", target)
    if priv:
        return int("-100" + priv.group(1))
    if pub and not pub.group(1).startswith("+") and pub.group(1) != "joinchat":
        return pub.group(1)
    if re.fullmatch(r"-?\d+", target):
        return int(target)
    if re.match(r"^[\w.\-]{4,}$", target):
        return target
    return None


def normalize_url(url: str) -> str:
    """Canonical form used for duplicate detection.

    Drops tracking query strings/fragments, 'www.' and trailing '/', maps twitter.com -> x.com
    and cuts tweet links after the status id (so /photo/1 and ?s=20 variants match).
    """
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host == "twitter.com":
        host = "x.com"
    path = parts.path.rstrip("/")
    if host == "x.com":
        m = re.match(r"(/[^/]+/status/\d+)", path)
        if m:
            path = m.group(1)
    if host in ("instagram.com", "instagr.am"):
        host = "instagram.com"
        # the same post can be linked as /p/, /reel/, /reels/, /tv/ or /<user>/reel/ - one shortcode
        m = re.match(r"(?:/[^/]+)?/(?:p|reels?|tv)/([^/]+)", path)
        if m:
            path = f"/p/{m.group(1)}"
    return f"https://{host}{path}"


def wants_force(text: str) -> bool:
    """True if the message contains the word 'force' (an Instagram profile then starts over, ignoring posts already sent)."""
    return bool(re.search(r"(?i)(?:^|\s)force(?:\s|$)", text or ""))
