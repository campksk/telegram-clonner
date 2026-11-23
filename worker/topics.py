import asyncio
import random
import logging

from telethon.tl.functions.messages import CreateForumTopicRequest, GetForumTopicsRequest
from telethon.tl.functions.messages import ForwardMessagesRequest
from telethon.tl.types import MessageActionTopicCreate, UpdateMessageID

from config import DEST_GROUP_ID, user_client

log = logging.getLogger("clone-bot.worker.topics")


TOPICS_PAGE = 100


async def _iter_topics(dest_entity, q: str | None = None):
    """Yield every forum topic of the destination group, page by page.

    Telegram paginates topics by the top message of the last topic seen (offset_date + offset_id)
    plus that topic's id (offset_topic), not by the topic id alone. A `seen` set guarantees the
    loop ends even if the server returns a page we already saw.
    """
    offset_date, offset_id, offset_topic = 0, 0, 0
    seen: set[int] = set()
    while True:
        kwargs = dict(peer=dest_entity, offset_date=offset_date, offset_id=offset_id,
                      offset_topic=offset_topic, limit=TOPICS_PAGE)
        if q is not None:
            kwargs["q"] = q
        result = await user_client(GetForumTopicsRequest(**kwargs))
        page = list(result.topics)
        fresh = [t for t in page if t.id not in seen]
        if not fresh:
            return
        for t in fresh:
            seen.add(t.id)
            yield t
        if len(page) < TOPICS_PAGE:
            return
        last = page[-1]
        top = next((m for m in result.messages if m.id == getattr(last, "top_message", None)), None)
        if top is None:
            return  # can't compute the next offset safely
        offset_date, offset_id, offset_topic = top.date, top.id, last.id


def _topic_id_from_updates(created) -> int:
    """The id of a newly created topic is the id of its 'topic created' service message."""
    updates = getattr(created, "updates", None) or []
    for u in updates:
        msg = getattr(u, "message", None)
        if isinstance(getattr(msg, "action", None), MessageActionTopicCreate):
            return msg.id
    for u in updates:
        if isinstance(u, UpdateMessageID):
            return u.id
    raise RuntimeError("Telegram did not return the id of the new topic")


_topic_locks: dict[tuple, asyncio.Lock] = {}


def _lock_for(title: str) -> asyncio.Lock:
    # one lock per (event loop, title): different topics never wait for each other
    return _topic_locks.setdefault((asyncio.get_running_loop(), title), asyncio.Lock())


async def get_or_create_topic(group_title: str) -> int:
    """Find the destination topic called `group_title`, creating it if needed.

    Serialised per title, so two jobs that need the same new topic at the same moment (say two X
    links sent back to back) don't both create it.
    """
    async with _lock_for(group_title.strip().lower()):
        return await _find_or_create_topic(group_title)


async def _find_or_create_topic(group_title: str) -> int:
    dest_entity = await user_client.get_entity(DEST_GROUP_ID)
    wanted = group_title.strip().lower()

    def matches(topic) -> bool:
        title = getattr(topic, "title", None)  # deleted topics have no title
        return bool(title) and title.strip().lower() == wanted

    # Pass 1: server-side search (fast)
    try:
        async for topic in _iter_topics(dest_entity, q=group_title):
            if matches(topic):
                log.info(f"Found topic: '{topic.title}' (id={topic.id})")
                return topic.id
    except Exception as e:
        log.debug(f"topic search failed, falling back to full scan: {e}")

    # Pass 2: full scan without q
    async for topic in _iter_topics(dest_entity):
        if matches(topic):
            log.info(f"Found topic (full scan): '{topic.title}' (id={topic.id})")
            return topic.id

    log.info(f"Creating new topic '{group_title}'...")
    created = await user_client(CreateForumTopicRequest(peer=dest_entity, title=group_title.strip()))
    return _topic_id_from_updates(created)


async def forward_to_topic(from_peer, dest, msg_ids: list[int], thread_id: int) -> None:
    await user_client(ForwardMessagesRequest(
        from_peer=from_peer, id=msg_ids, to_peer=dest, top_msg_id=thread_id,
        random_id=[random.randint(-(2**63), 2**63 - 1) for _ in msg_ids],  # signed 64-bit
        silent=False, drop_author=False
    ))
