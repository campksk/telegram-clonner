import asyncio
import datetime
import types

import pytest
from telethon.tl.functions.messages import CreateForumTopicRequest, ForwardMessagesRequest, GetForumTopicsRequest
from telethon.tl.types import ForumTopicDeleted, MessageActionTopicCreate, UpdateMessageID

from worker import topics

DATE = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)


def make_topic(i):
    # topic i was last active at message 1000+i; higher id = more recent
    return types.SimpleNamespace(id=i, title=f"Topic {i}", top_message=1000 + i)


class FakeServer:
    """Models getForumTopics as documented: newest activity first, paginated by (offset_id, offset_topic)."""

    def __init__(self, topic_list, honour_offsets=True, search_fails=False):
        self.topics = sorted(topic_list, key=lambda t: -getattr(t, "top_message", 0))
        self.honour_offsets = honour_offsets
        self.search_fails = search_fails
        self.requests = []
        self.created = []
        self.create_result = None

    async def get_entity(self, ref):
        return types.SimpleNamespace(id=1)

    async def __call__(self, request):
        self.requests.append(request)
        if isinstance(request, GetForumTopicsRequest):
            if request.q and self.search_fails:
                raise RuntimeError("search unavailable")
            pool = self.topics
            if request.q:
                pool = [t for t in pool if request.q.lower() in getattr(t, "title", "").lower()]
            if self.honour_offsets and request.offset_id:
                pool = [t for t in pool if getattr(t, "top_message", 0) < request.offset_id]
            page = pool[: request.limit]
            msgs = [types.SimpleNamespace(id=t.top_message, date=DATE) for t in page if hasattr(t, "top_message")]
            return types.SimpleNamespace(topics=page, messages=msgs)
        if isinstance(request, CreateForumTopicRequest):
            self.created.append(request.title)
            return self.create_result
        if isinstance(request, ForwardMessagesRequest):
            return types.SimpleNamespace()
        raise AssertionError(request)


@pytest.fixture
def server(monkeypatch):
    srv = FakeServer([])
    monkeypatch.setattr(topics, "user_client", srv)
    return srv


def test_finds_existing_topic_case_insensitively(server):
    server.topics = [make_topic(i) for i in range(1, 6)]
    assert asyncio.run(topics.get_or_create_topic("  topic 3 ")) == 3
    assert server.created == []


def test_full_scan_paginates_past_100_topics(server):
    server.topics = sorted([make_topic(i) for i in range(1, 251)], key=lambda t: -t.top_message)
    server.search_fails = True                      # force pass 2 (full scan)
    assert asyncio.run(asyncio.wait_for(topics.get_or_create_topic("Topic 7"), 5)) == 7   # oldest = 3rd page
    pages = [r for r in server.requests if not r.q]
    assert len(pages) == 3
    # next page is requested from the top message of the last topic of the previous page
    assert pages[1].offset_id == 1000 + 151 and pages[1].offset_topic == 151


def test_server_ignoring_offsets_cannot_loop_forever(server):
    server.topics = [make_topic(i) for i in range(1, 151)]
    server.honour_offsets = False
    server.search_fails = True
    server.create_result = types.SimpleNamespace(updates=[UpdateMessageID(id=9, random_id=1)])
    assert asyncio.run(asyncio.wait_for(topics.get_or_create_topic("Not there"), 5)) == 9
    assert server.created == ["Not there"]


def test_deleted_topics_do_not_crash_the_search(server):
    server.topics = [ForumTopicDeleted(id=50), make_topic(2), make_topic(3)]
    assert asyncio.run(topics.get_or_create_topic("Topic 3")) == 3
    server.search_fails = True
    assert asyncio.run(topics.get_or_create_topic("Topic 2")) == 2


def test_new_topic_id_comes_from_the_topic_created_service_message(server):
    service = types.SimpleNamespace(message=types.SimpleNamespace(id=77, action=MessageActionTopicCreate(title="T", icon_color=0)))
    unrelated = UpdateMessageID(id=5, random_id=1)             # first update is NOT the topic id
    server.create_result = types.SimpleNamespace(updates=[unrelated, service])
    assert asyncio.run(topics.get_or_create_topic("T")) == 77


def test_unknown_create_response_is_an_error_not_a_guess(server):
    server.create_result = types.SimpleNamespace(updates=[])
    with pytest.raises(RuntimeError):
        asyncio.run(topics.get_or_create_topic("T"))


def test_forward_random_ids_fit_signed_64_bit(server, monkeypatch):
    monkeypatch.setattr(topics.random, "randint", lambda a, b: b)     # the worst case
    asyncio.run(topics.forward_to_topic(object(), object(), [1, 2, 3], 7))
    ids = server.requests[-1].random_id
    assert len(ids) == 3 and all(-(2**63) <= i <= 2**63 - 1 for i in ids)
