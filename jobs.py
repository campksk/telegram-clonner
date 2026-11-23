"""Tracked background jobs.

Every job (single message, X/Instagram/TikTok link, whole group) runs through start_job so that it
- is kept in active_tasks (visible in /jobs, stoppable with /cancel, never garbage-collected),
- reports unexpected errors to the user instead of dying silently,
- is removed from active_tasks when finished.

Group clones are also written to the pending-jobs file so they resume after a restart.
"""
import asyncio
import logging
from typing import Awaitable, Callable

import db
import worker
from config import active_tasks
from parsing import parse_source

log = logging.getLogger("clone-bot.jobs")
StatusCb = Callable[[str], Awaitable[None]]

NOTIFY_TIMEOUT = 5  # seconds; status messages are best-effort and must never stall shutdown


def is_running(key: str) -> bool:
    task = active_tasks.get(key)
    return task is not None and not task.done()


async def _notify(status_cb: StatusCb, text: str) -> None:
    try:
        await asyncio.wait_for(status_cb(text), NOTIFY_TIMEOUT)
    except Exception as e:  # incl. timeout; CancelledError is deliberately not swallowed
        log.debug(f"status message not delivered: {e}")


def start_job(
    key: str,
    label: str,
    coro: Awaitable,
    status_cb: StatusCb,
    *,
    pending: bool = False,
    on_success: Callable[[], Awaitable[None]] | None = None,
) -> asyncio.Task:
    """Run `coro` as a tracked job under `key`.

    pending=True: the job is in the pending-jobs file. It is removed when the job finishes or fails,
    but NOT when the process is shutting down (task cancelled) - that is what lets it resume on the
    next start. An explicit /cancel removes it itself (see handlers.system).
    on_success runs only if the coroutine returned True.
    """

    async def runner():
        try:
            result = await coro
        except asyncio.CancelledError:
            await _notify(status_cb, f"🛑 Cancelled {label}")
            raise
        except Exception as e:
            log.exception(f"job {key} failed")
            await _notify(status_cb, f"❌ {label} failed: `{e}`")
            if pending:
                db.remove_pending_job(key)  # don't retry a crashing job forever
        else:
            if pending:
                db.remove_pending_job(key)
            if on_success is not None and result is True:
                try:
                    await on_success()
                except Exception:
                    log.exception(f"job {key}: on_success failed")
        finally:
            if active_tasks.get(key) is asyncio.current_task():
                del active_tasks[key]

    task = asyncio.create_task(runner())
    active_tasks[key] = task
    return task


def start_group_job(key: str, source, status_cb: StatusCb) -> asyncio.Task:
    db.add_pending_job(key, source)
    return start_job(key, f"clone `{key}`", worker.clone_group(source, status_cb), status_cb, pending=True)


def resume_pending_jobs(status_cb_for: Callable[[str], StatusCb]) -> int:
    """Restart group clones that were interrupted by a restart. Returns how many were started."""
    started = 0
    for job in db.load_pending_jobs():
        key = str(job.get("source"))
        if is_running(key):
            continue
        source = job["ref"] if "ref" in job else parse_source(key)  # old entries have no 'ref'
        if source is None:
            log.warning(f"dropping unreadable pending job: {job!r}")
            db.remove_pending_job(key)
            continue
        start_group_job(key, source, status_cb_for(key))
        started += 1
    return started
