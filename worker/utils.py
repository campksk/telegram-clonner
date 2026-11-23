import asyncio
import re
import logging
from telethon.errors import FloodWaitError
from telethon.errors.rpcbaseerrors import FloodError
try:
    from telethon.errors import FloodPremiumWaitError
except ImportError:
    FloodPremiumWaitError = FloodError
from telethon.tl.types import User as TLUser

log = logging.getLogger("clone-bot.worker")

STICKER_MIMES = {"image/webp", "application/x-tgsticker"}

FLOOD_ERRORS = (FloodWaitError, FloodPremiumWaitError)
FLOOD_MARGIN = 2    # extra seconds on top of what Telegram asks for
FLOOD_RETRIES = 3   # how many waits to sit out before giving up on one request
_sleep = asyncio.sleep


def _flood_seconds(e: Exception, default: int = 5) -> int:
    if hasattr(e, "seconds"): return e.seconds
    m = re.search(r"(\d+)\s*seconds?", str(e))
    return int(m.group(1)) if m else default


async def retry_flood(op, what: str = "request"):
    """Run `await op()`; if Telegram answers FloodWait, sleep as long as it asks and try again.

    Gives up (re-raises the FloodWait) after FLOOD_RETRIES waits. Other errors propagate at once.
    `op` must be a zero-argument callable returning a new awaitable each time.
    """
    for attempt in range(FLOOD_RETRIES + 1):
        try:
            return await op()
        except FLOOD_ERRORS as e:
            if attempt == FLOOD_RETRIES:
                raise
            wait = _flood_seconds(e) + FLOOD_MARGIN
            log.warning(f"FloodWait during {what}: sleeping {wait}s (retry {attempt + 1}/{FLOOD_RETRIES})")
            await _sleep(wait)


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
