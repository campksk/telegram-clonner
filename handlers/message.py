import hashlib
import logging
import re
from telethon import events

import db
import jobs
import worker
from config import bot_client, is_allowed
from parsing import (
    IG_POST_RE, MESSAGE_PATTERN, canonical_instagram, extract_target, instagram_profile, is_instagram,
    normalize_url, parse_source, parse_tg_link, wants_force,
)

log = logging.getLogger("clone-bot.message")

X_RE = re.compile(r"https?://(?:www\.)?(?:twitter\.com|x\.com)/([^/]+)/status/(\d+)")
TIKTOK_RE = re.compile(r"https?://(?:www\.|vm\.|vt\.)?tiktok\.com/\S+")


def _site_job(arg: str, force: bool):
    """Which download job a link belongs to, or None.

    -> (task key, label, run(status_cb) -> coroutine, url to remember once the job succeeded or None).
    Links are never refused as duplicates. Instagram posts are remembered only so that a later run
    over a whole profile skips posts that were already sent.
    """
    m = X_RE.search(arg)
    if m:
        return f"x_{m.group(2)}", "X", lambda cb: worker.clone_x_media(arg, cb), None
    m = IG_POST_RE.search(arg)
    if m:
        return f"ig_{m.group(1)}", "Instagram", lambda cb: worker.clone_ig_media(arg, cb), normalize_url(arg)
    username = instagram_profile(arg)
    if username:
        return (f"igprofile_{username.lower()}", "Instagram profile",
                lambda cb: worker.clone_ig_profile(username, cb, force=force), None)
    if TIKTOK_RE.search(arg):
        digest = hashlib.md5(normalize_url(arg).encode()).hexdigest()[:12]
        return f"tiktok_{digest}", "TikTok", lambda cb: worker.clone_tiktok_media(arg, cb), None
    return None


@bot_client.on(events.NewMessage(pattern=MESSAGE_PATTERN))
async def handle_message(event: events.NewMessage.Event):
    # Private chats only
    if not event.is_private:
        return

    if not is_allowed(event.sender_id):
        return

    # Use only the first URL of the message (links are often pasted with extra text or on
    # several lines); a bare ID/username with no URL is kept as-is.
    raw = event.pattern_match.group(1) or ""
    arg = extract_target(raw)
    if not arg: return
    if is_instagram(arg):
        arg = canonical_instagram(arg)

    status_msg = await event.reply(f"🚀 Processing `{arg}`...")
    async def status_cb(text: str):
        try: await status_msg.edit(text, parse_mode="md")
        except Exception: await event.respond(text, parse_mode="md")

    # --- one message: t.me/<chat>/<id>, t.me/c/<chat>/<id> ---
    link_parsed = parse_tg_link(arg)
    if link_parsed:
        chat_ref, msg_id = link_parsed
        key = f"msg_{chat_ref}_{msg_id}"
        if jobs.is_running(key):
            await event.reply("⚠️ This message is already being cloned")
            return
        jobs.start_job(key, f"message `{msg_id}`", worker.clone_single_message(chat_ref, msg_id, status_cb), status_cb)
        return

    # --- X / Instagram / TikTok ---
    site = _site_job(arg, wants_force(raw))
    if site:
        key, label, run, remember_url = site
        if jobs.is_running(key):
            await event.reply(f"⚠️ This {label} link is already being downloaded...")
            return
        on_success = None
        if remember_url:
            async def mark_sent():
                await db.mark_url_sent(remember_url)

            on_success = mark_sent

        jobs.start_job(key, f"{label} download", run(status_cb), status_cb, on_success=on_success)
        return

    # --- an Instagram link we can't download (explore, tags, ...) ---
    if is_instagram(arg):
        await status_cb(f"⚠️ Instagram link not supported: `{arg}`\nSend a link to a post, reel, story or profile")
        return

    # --- whole group / channel / user ---
    source = parse_source(arg)
    if source is None:
        await status_cb(f"⚠️ not recognized: `{arg}`\nSend a t.me link, numeric ID, or username")
        return

    key = str(arg)
    if jobs.is_running(key):
        await event.reply(f"⚠️ `{arg}` is already being cloned. Send `/cancel` to stop it")
        return
    jobs.start_group_job(key, source, status_cb)
