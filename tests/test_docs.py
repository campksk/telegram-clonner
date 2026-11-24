"""Guards for the documentation and for the 'everything is in English' rule."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "venv", "__pycache__", "sessions", "downloads", ".pytest_cache"}
THAI = re.compile("[\u0E00-\u0E7F]")


def text_files():
    for p in sorted(ROOT.rglob("*")):
        if p.is_file() and not SKIP_DIRS & set(p.parts) \
                and (p.suffix in {".py", ".md", ".txt", ".sh", ".ini"} or p.name == ".env.example"):
            yield p


def test_no_thai_text_left_in_the_project():
    found = {}
    for p in text_files():
        lines = [n for n, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1) if THAI.search(line)]
        if lines:
            found[str(p.relative_to(ROOT))] = lines[:5]
    assert not found, f"Thai text found (lines): {found}"


def test_readme_documents_every_setting():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    config_py = (ROOT / "config.py").read_text(encoding="utf-8")
    for name in ("API_ID", "API_HASH", "BOT_TOKEN", "DEST_GROUP_ID", "ALLOWED_USER_IDS", "SEND_DELAY",
                 "PARALLEL_WORKERS", "PRESERVE_ORDER", "MAX_SCAN_MESSAGES", "PROFILE_MAX_POSTS", "PROFILE_POST_DELAY",
                 "DOWNLOAD_DIR", "SESSION_DIR"):
        assert f"| {name} |" in readme, f"{name} missing from README"
        assert name in config_py, f"{name} not read by config.py"
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for name in ("API_ID", "API_HASH", "BOT_TOKEN", "DEST_GROUP_ID", "ALLOWED_USER_IDS", "MAX_SCAN_MESSAGES",
                 "PROFILE_MAX_POSTS", "PROFILE_POST_DELAY"):
        assert re.search(rf"^{name}=", example, re.M), f"{name} missing from .env.example"
