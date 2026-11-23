"""Sending downloaded media with its caption."""
import logging

log = logging.getLogger("clone-bot.worker.sender")

CAPTION_LIMIT = 1024    # Telegram's media caption limit for regular accounts
MESSAGE_LIMIT = 4096    # Telegram's text message limit


def pick_caption(batch) -> str:
    """The caption of a message group.

    In an album only one message carries the text, and it is usually the first one, not the last.
    """
    for msg in batch:
        text = getattr(msg, "text", None)
        if text:
            return text
    return ""


def split_text(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Split `text` into chunks of at most `limit` characters, preferring line breaks."""
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


async def send_media(client, dest, paths: list[str], caption: str, thread_id: int) -> None:
    """Send one file or an album (max 10) to `dest`/topic with `caption`.

    A caption longer than CAPTION_LIMIT would make Telegram reject the whole upload, so in that case
    the media is sent without a caption and the full text follows as normal message(s).
    """
    caption = caption or ""
    overflow = ""
    if len(caption) > CAPTION_LIMIT:
        overflow, caption = caption, ""

    if len(paths) == 1:
        await client.send_file(dest, file=paths[0], caption=caption, reply_to=thread_id, parse_mode="md")
    elif len(paths) > 1:
        captions = [caption] + [""] * (len(paths) - 1)
        await client.send_file(dest, file=paths, caption=captions, reply_to=thread_id, parse_mode="md")
    else:
        return

    for chunk in split_text(overflow):
        await client.send_message(dest, chunk, reply_to=thread_id, parse_mode="md")
