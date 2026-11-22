import asyncio
import os
import subprocess
import sys
import types
from pathlib import Path

import config
from handlers import message, system

ROOT = Path(__file__).resolve().parent.parent


class FakeEvent:
    def __init__(self, sender_id):
        self.sender_id = sender_id
        self.is_private = True
        self.replies = []

    async def reply(self, text, **kwargs):
        self.replies.append(text)
        return types.SimpleNamespace(edit=None)


def test_is_allowed_fails_closed(monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_USERS", {1, 2})
    assert config.is_allowed(1) and not config.is_allowed(3) and not config.is_allowed(None)
    monkeypatch.setattr(config, "ALLOWED_USERS", set())
    assert not config.is_allowed(1)


def test_handlers_ignore_everyone_when_allowlist_empty(monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_USERS", set())
    for handler in (system.handle_ping, system.handle_jobs, system.handle_cancel):
        ev = FakeEvent(5)
        ev.pattern_match = types.SimpleNamespace(group=lambda i: "")
        asyncio.run(handler(ev))
        assert ev.replies == []
    ev = FakeEvent(5)
    ev.pattern_match = types.SimpleNamespace(group=lambda i: "https://t.me/durov")
    asyncio.run(message.handle_message(ev))
    assert ev.replies == []


def test_allowed_user_gets_a_reply(monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_USERS", {5})
    ev = FakeEvent(5)
    asyncio.run(system.handle_jobs(ev))
    assert len(ev.replies) == 1


def test_main_refuses_to_start_without_allowlist():
    env = dict(os.environ, ALLOWED_USER_IDS="")
    r = subprocess.run([sys.executable, "main.py"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 1
    assert "ALLOWED_USER_IDS" in (r.stdout + r.stderr)
