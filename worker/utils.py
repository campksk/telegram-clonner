import re
import logging
from telethon.tl.types import User as TLUser

log = logging.getLogger("clone-bot.worker")

STICKER_MIMES = {"image/webp", "application/x-tgsticker"}


def _flood_seconds(e: Exception, default: int = 5) -> int:
    if hasattr(e, "seconds"): return e.seconds
    m = re.search(r"(\d+)\s*seconds?", str(e))
    return int(m.group(1)) if m else default


def is_supported_media(msg) -> bool:
    if msg.sticker: return False
    if msg.photo: return True
    if msg.document:
        mime = (msg.document.mime_type or "").lower()
        if mime in STICKER_MIMES: return False
        return True
    return False


def is_public_entity(entity) -> bool:
    if isinstance(entity, TLUser): return False
    return not bool(getattr(entity, "noforwards", False))
