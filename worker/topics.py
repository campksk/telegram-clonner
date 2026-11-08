import random
import logging
from typing import List

from telethon.tl.functions.messages import CreateForumTopicRequest, GetForumTopicsRequest
from telethon.tl.functions.messages import ForwardMessagesRequest

from config import DEST_GROUP_ID, user_client

log = logging.getLogger("clone-bot.worker.topics")


async def get_or_create_topic(group_title: str) -> int:
    dest_entity = await user_client.get_entity(DEST_GROUP_ID)
    search_title = group_title.strip().lower()

    # Pass 1: q search (fast)
    try:
        result = await user_client(GetForumTopicsRequest(
            peer=dest_entity, offset_date=0, offset_id=0, offset_topic=0, limit=100, q=group_title
        ))
        for topic in result.topics:
            if topic.title.strip().lower() == search_title:
                log.info(f"Found topic: '{topic.title}' (id={topic.id})")
                return topic.id
    except Exception:
        pass

    # Pass 2: full scan without q (paginate all topics)
    offset_id = 0
    while True:
        result = await user_client(GetForumTopicsRequest(
            peer=dest_entity, offset_date=0, offset_id=offset_id, offset_topic=0, limit=100
        ))
        if not result.topics:
            break
        for topic in result.topics:
            if topic.title.strip().lower() == search_title:
                log.info(f"Found topic (full scan): '{topic.title}' (id={topic.id})")
                return topic.id
        offset_id = result.topics[-1].id

    log.info(f"Creating new topic '{group_title}'...")
    created = await user_client(CreateForumTopicRequest(peer=dest_entity, title=group_title.strip()))
    return created.updates[0].id


async def forward_to_topic(from_peer, dest, msg_ids: list[int], thread_id: int) -> None:
    await user_client(ForwardMessagesRequest(
        from_peer=from_peer, id=msg_ids, to_peer=dest, top_msg_id=thread_id,
        random_id=[random.randint(0, 2**63) for _ in msg_ids],
        silent=False, drop_author=False
    ))
