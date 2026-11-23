import asyncio

import pytest
from telethon.errors import FloodWaitError

from worker import utils
from worker.utils import FloodPremiumWaitError, retry_flood


def flaky(failures, error=lambda: FloodWaitError(request=None, capture=7)):
    calls = []

    async def op():
        calls.append(1)
        if len(calls) <= failures:
            raise error()
        return "ok"

    return op, calls


def test_retries_after_the_wait_telegram_asked_for(flood_sleeps):
    op, calls = flaky(2)
    assert asyncio.run(retry_flood(op)) == "ok"
    assert len(calls) == 3 and flood_sleeps == [7, 7]


def test_adds_the_safety_margin(flood_sleeps, monkeypatch):
    monkeypatch.setattr(utils, "FLOOD_MARGIN", 2)
    op, _ = flaky(1)
    asyncio.run(retry_flood(op))
    assert flood_sleeps == [9]


def test_premium_flood_errors_are_retried_too(flood_sleeps):
    op, calls = flaky(1, error=lambda: FloodPremiumWaitError(request=None, capture=3))
    assert asyncio.run(retry_flood(op)) == "ok" and flood_sleeps == [3]


def test_gives_up_after_the_retry_budget(flood_sleeps):
    op, calls = flaky(100)
    with pytest.raises(FloodWaitError):
        asyncio.run(retry_flood(op))
    assert len(calls) == utils.FLOOD_RETRIES + 1 and len(flood_sleeps) == utils.FLOOD_RETRIES


def test_other_errors_are_not_retried(flood_sleeps):
    op, calls = flaky(5, error=lambda: ValueError("nope"))
    with pytest.raises(ValueError):
        asyncio.run(retry_flood(op))
    assert len(calls) == 1 and flood_sleeps == []
