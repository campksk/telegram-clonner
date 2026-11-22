"""Pure text-parsing helpers (no Telegram/network imports, so they are easy to test)."""
import re

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
