import asyncio
import logging
import shutil
from pathlib import Path
from typing import Callable, Awaitable

from telethon.errors import ChatAdminRequiredError, ChatWriteForbiddenError
from telethon.tl.types import User as TLUser

import db
from config import DEST_GROUP_ID, DOWNLOAD_DIR, SEND_DELAY, PARALLEL_WORKERS, PRESERVE_ORDER, MAX_SCAN_MESSAGES, user_client

from worker.utils import FLOOD_ERRORS, retry_flood, is_supported_media, is_public_entity
from worker.sender import pick_caption, send_media
from worker.topics import get_or_create_topic, forward_to_topic

log = logging.getLogger("clone-bot.worker.clone")

DOWNLOAD_RETRY_DELAY = 2  # seconds, multiplied by the attempt number

# Forward errors where copying the media instead cannot help (flooding / no right to post in the destination)
NO_FALLBACK = (*FLOOD_ERRORS, ChatAdminRequiredError, ChatWriteForbiddenError)
StatusCb = Callable[[str], Awaitable[None]]


async def clone_group(source: str | int, status_cb: StatusCb) -> None:
    try:
        source_entity = await user_client.get_entity(source)
    except ValueError:
        await status_cb(f"🔄 Updating chat list to find {source} ...")
        await user_client.get_dialogs()
        try:
            source_entity = await user_client.get_entity(source)
        except Exception as e:
            await status_cb(f"❌ `{source}` not found (User account must be a member of this group/channel)\n`{e}`")
            return
    except Exception as e:
        await status_cb(f"❌ `{source}` not found\n`{e}`")
        return

    if isinstance(source_entity, TLUser):
        parts = [source_entity.first_name or "", source_entity.last_name or ""]
        title = " ".join(p for p in parts if p).strip() or str(source_entity.id)
        entity_type = "user"
    else:
        title = getattr(source_entity, "title", str(source))
        entity_type = "group/channel"

    src_key = str(source_entity.id)
    already_sent = db.get_sent(src_key)
    await status_cb(f"🔍 Found {entity_type}: **{title}**\nCounting media...")

    try:
        thread_id = await get_or_create_topic(title)
    except ChatAdminRequiredError:
        await status_cb("❌ Bot must be admin in destination group and Topics must be enabled")
        return
    except Exception as e:
        await status_cb(f"❌ Failed to create/find topic: `{e}`")
        return

    raw_messages = []
    limit_hit = False
    async for msg in user_client.iter_messages(source_entity, reverse=True):
        if not is_supported_media(msg) or msg.id in already_sent:
            continue
        # MAX_SCAN_MESSAGES caps the NEW media taken per run. Already-sent messages don't count
        # (otherwise chats with more than the limit could never reach their newest media), and an
        # album that straddles the limit is finished rather than split.
        if len(raw_messages) >= MAX_SCAN_MESSAGES and not (
            msg.grouped_id and msg.grouped_id == raw_messages[-1].grouped_id
        ):
            limit_hit = True
            log.warning(f"clone_group: reached limit of {MAX_SCAN_MESSAGES} new media for this run")
            break
        raw_messages.append(msg)

    batches, album_buf, seen_albums = [], {}, []
    for msg in raw_messages:
        gid = msg.grouped_id
        if gid:
            if gid not in album_buf:
                album_buf[gid] = []
                seen_albums.append(gid)
            album_buf[gid].append(msg)
        else:
            for aid in seen_albums: batches.append(album_buf.pop(aid))
            seen_albums.clear()
            batches.append([msg])
    for aid in seen_albums: batches.append(album_buf.pop(aid))

    total_files, total_batches, skipped = len(raw_messages), len(batches), len(already_sent)
    if total_files == 0:
        skip_note = f" ({skipped} files already sent)" if skipped else ""
        await status_cb(f"ℹ️ No new media to send{skip_note}")
        return

    dest_entity = await user_client.get_entity(DEST_GROUP_ID)
    use_forward = is_public_entity(source_entity)
    mode_label = "⚡ forward" if use_forward else f"📥 download→upload (workers={PARALLEL_WORKERS})"
    skip_note = f" | {skipped} files already sent" if skipped else ""
    
    await status_cb(
        f"📦 Found **{total_files}** new media files ({total_batches} groups){skip_note}\n"
        f"📤 mode: {mode_label} → topic **{title}**..."
    )

    sem = asyncio.Semaphore(PARALLEL_WORKERS)
    counters = {"success": 0, "failed": 0, "done": 0}
    lock = asyncio.Lock()
    progress_lock = asyncio.Lock()

    async def _update_progress():
        done_now = counters["done"]
        if done_now % 10 == 0 or done_now == total_batches:
            await db.save_sent()  # checkpoint so a crash/kill loses at most ~10 groups of progress
            async with progress_lock:
                await status_cb(
                    f"⏳ Progress: {counters['success'] + counters['failed']}/{total_files} files "
                    f"| {done_now}/{total_batches} groups "
                    f"(✓{counters['success']} ✗{counters['failed']})"
                )

    job_dir = DOWNLOAD_DIR / src_key  # created on first download (forward mode may never need it)

    # Batches are downloaded/prepared in parallel but SENT in message order (when PRESERVE_ORDER):
    # batch i may only send after batch i-1 has finished its send step. Each batch holds its
    # semaphore slot until it has sent, and slots are handed out in batch order, so the oldest
    # unsent batch always has a slot and this cannot deadlock.
    turns = [asyncio.Event() for _ in range(len(batches) + 1)]
    turns[0].set()

    async def _download(b_idx, batch):
        """Download a batch. Returns [(message, path)] for the files that arrived."""
        job_dir.mkdir(parents=True, exist_ok=True)
        got = []
        for msg in batch:
            for attempt in range(3):
                try:
                    p = await user_client.download_media(msg, file=str(job_dir) + "/")
                    if p: got.append((msg, p))
                    break
                except Exception as e:
                    if attempt == 2:
                        log.warning(f"[up {b_idx}] skip msg {msg.id}: {e}")
                    else:
                        await asyncio.sleep(DOWNLOAD_RETRY_DELAY * (attempt + 1))
        return got

    async def process_batch(b_idx, batch):
        got, delivered = [], []
        async with sem:
            if not use_forward:
                got = await _download(b_idx, batch)
            if PRESERVE_ORDER:
                await turns[b_idx - 1].wait()
            try:
                if use_forward:
                    try:
                        await retry_flood(
                            lambda: forward_to_topic(source_entity, dest_entity, [m.id for m in batch], thread_id),
                            f"forward {b_idx}")
                        delivered = list(batch)
                    except NO_FALLBACK:
                        raise
                    except Exception as e:
                        # e.g. a message that can't be forwarded (protected content): copy it instead
                        log.warning(f"[fwd {b_idx}] forward failed ({e}); falling back to download→upload")
                        got = await _download(b_idx, batch)
                        if got:
                            await send_media(user_client, dest_entity, [p for _, p in got], pick_caption(batch), thread_id)
                            delivered = [m for m, _ in got]
                elif got:
                    await send_media(user_client, dest_entity, [p for _, p in got], pick_caption(batch), thread_id)
                    delivered = [m for m, _ in got]
            except asyncio.CancelledError:
                raise
            except Exception as e:  # incl. a FloodWait that outlasted all retries
                log.error(f"[{'fwd' if use_forward else 'up'} {b_idx}] ✗ {e}")
            finally:
                turns[b_idx].set()  # always release the next batch, even if this one failed
                for _, p in got: Path(p).unlink(missing_ok=True)

            # Only what was really delivered is marked as sent; the rest counts as failed
            # (and is picked up again on the next run).
            async with lock:
                if delivered: db.mark_sent_sync(src_key, {m.id for m in delivered})
                counters["success"] += len(delivered)
                counters["failed"] += len(batch) - len(delivered)
                counters["done"] += 1
            await _update_progress()
            await asyncio.sleep(SEND_DELAY / PARALLEL_WORKERS)

    try:
        await asyncio.gather(*[process_batch(i, b) for i, b in enumerate(batches, 1)])
    finally:
        # Runs on success, /cancel and errors alike, so already-sent IDs are never forgotten
        # (otherwise a cancelled job re-sends everything next time).
        await db.save_sent()
        shutil.rmtree(job_dir, ignore_errors=True)

    await status_cb(
        f"✅ Clone complete!\n• Source: **{title}**\n• Mode: {mode_label}\n• Topic: `{thread_id}`\n"
        f"• Success: {counters['success']}/{total_files} files\n• Failed: {counters['failed']}/{total_files} files"
        + (f"\n⚠️ Stopped at the MAX_SCAN_MESSAGES limit ({MAX_SCAN_MESSAGES} files per run) — send the same source again to continue" if limit_hit else "")
    )


async def clone_single_message(chat: str | int, msg_id: int, status_cb: StatusCb) -> None:
    try:
        chat_entity = await user_client.get_entity(chat)
    except ValueError:
        await status_cb(f"🔄 Updating chat list to find ID {chat} ...")
        await user_client.get_dialogs()
        try:
            chat_entity = await user_client.get_entity(chat)
        except Exception as e:
            await status_cb(f"❌ Chat `{chat}` not found (User account must be a member of this group/channel)\n`{e}`")
            return
    except Exception as e:
        await status_cb(f"❌ Chat `{chat}` not found\n`{e}`")
        return

    if isinstance(chat_entity, TLUser):
        parts = [chat_entity.first_name or "", chat_entity.last_name or ""]
        chat_title = " ".join(p for p in parts if p).strip() or str(chat_entity.id)
    else:
        chat_title = getattr(chat_entity, "title", str(chat))

    try:
        thread_id = await get_or_create_topic(chat_title)
    except Exception as e:
        await status_cb(f"❌ Failed to create/find topic: `{e}`")
        return

    msgs = await user_client.get_messages(chat_entity, ids=msg_id)
    if not msgs:
        await status_cb(f"❌ Message id `{msg_id}` not found in `{chat_title}`")
        return
    msg = msgs if not isinstance(msgs, list) else msgs[0]
    if not msg:
        await status_cb(f"❌ Message id `{msg_id}` not found")
        return

    batch = []
    if msg.grouped_id:
        async for m in user_client.iter_messages(chat_entity, min_id=msg_id - 20, max_id=msg_id + 20):
            if m.grouped_id == msg.grouped_id and is_supported_media(m):
                batch.append(m)
        batch.sort(key=lambda m: m.id)
    elif is_supported_media(msg):
        batch = [msg]
    else:
        await status_cb(f"⚠️ Message `{msg_id}` has no supported media")
        return

    await status_cb(f"🔍 Found: **{chat_title}** — message `{msg_id}`\n📤 Sending → topic **{chat_title}**...")
    dest_entity = await user_client.get_entity(DEST_GROUP_ID)
    use_forward = is_public_entity(chat_entity)
    
    if use_forward:
        try:
            await retry_flood(
                lambda: forward_to_topic(chat_entity, dest_entity, [m.id for m in batch], thread_id),
                "forward")
            actual_mode = "⚡ forward"
        except Exception as e:
            log.warning(f"single message: forward failed ({e}); falling back to download→upload")
            use_forward = False

    if not use_forward:
        job_dir = DOWNLOAD_DIR / f"single_{chat_entity.id}_{msg_id}"
        job_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        try:
            for m in batch:
                p = await user_client.download_media(m, file=str(job_dir) + "/")
                if p: paths.append(p)
            if not paths:
                await status_cb("❌ Could not download the media")
                return
            await send_media(user_client, dest_entity, paths, pick_caption(batch), thread_id)
            actual_mode = "📥 upload"
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)

    await db.mark_sent(str(chat_entity.id), {m.id for m in batch})  # so a later group clone doesn't send it again
    await status_cb(f"✅ Sent successfully!\n• Chat: **{chat_title}**\n• Files: {len(batch)}\n• Mode: {actual_mode}")
