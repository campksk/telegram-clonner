import os
import sys
import tempfile
from pathlib import Path

# Never touch real sessions/downloads/.env values while testing.
_tmp = tempfile.mkdtemp(prefix="tgclone-tests-")
os.environ.update(
    API_ID="1",
    API_HASH="test",
    BOT_TOKEN="123:test",
    DEST_GROUP_ID="-100123",
    ALLOWED_USER_IDS="1",
    SESSION_DIR=f"{_tmp}/sessions",
    DOWNLOAD_DIR=f"{_tmp}/downloads",
)
sys.path.insert(0, str(Path(__file__).parent))


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def flood_sleeps(monkeypatch):
    """FloodWait sleeps are recorded instead of slept, and carry no safety margin, so tests stay fast."""
    from worker import utils

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(utils, "_sleep", fake_sleep)
    monkeypatch.setattr(utils, "FLOOD_MARGIN", 0)
    return sleeps
