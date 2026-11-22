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
