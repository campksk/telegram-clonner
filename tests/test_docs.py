"""Guards against the garbled Thai (stacked tone marks) that used to fill the README."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# two tone marks in a row, the same combining mark twice, or a tone mark squeezed between two
# 'mai han-akat' - none of these occur in correctly typed Thai
STACKED = re.compile(
    "([\u0E31\u0E34-\u0E3A\u0E47-\u0E4E])\\1"
    "|[\u0E48-\u0E4B]{2}"
    "|[\u0E48-\u0E4B]\u0E31[\u0E48-\u0E4B]"
    "|\u0E31[\u0E48-\u0E4B]\u0E31"
)


def text_files():
    for p in sorted(ROOT.rglob("*")):
        if p.is_file() and not {".git", "venv", "__pycache__", "sessions", "downloads"} & set(p.parts) \
                and (p.suffix in {".py", ".md", ".txt", ".sh"} or p.name == ".env.example"):
            yield p


def test_no_stacked_thai_marks_anywhere():
    bad = {}
    for p in text_files():
        found = [m.group(0) for m in STACKED.finditer(p.read_text(encoding="utf-8", errors="ignore"))]
        if found:
            bad[str(p.relative_to(ROOT))] = len(found)
    # this file contains the pattern on purpose
    bad.pop("tests/test_docs.py", None)
    assert not bad, f"garbled Thai text in: {bad}"


def test_readme_documents_every_setting():
    import config  # noqa: F401  (settings live in config.py)
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for name in ("API_ID", "API_HASH", "BOT_TOKEN", "DEST_GROUP_ID", "ALLOWED_USER_IDS", "SEND_DELAY",
                 "PARALLEL_WORKERS", "PRESERVE_ORDER", "MAX_SCAN_MESSAGES", "PROFILE_MAX_POSTS", "PROFILE_POST_DELAY",
                 "DOWNLOAD_DIR", "SESSION_DIR"):
        assert f"| {name} |" in readme, f"{name} missing from README"
        assert name in (ROOT / "config.py").read_text(encoding="utf-8"), f"{name} not read by config.py"
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for name in ("API_ID", "API_HASH", "BOT_TOKEN", "DEST_GROUP_ID", "ALLOWED_USER_IDS", "MAX_SCAN_MESSAGES"):
        assert re.search(rf"^{name}=", example, re.M), f"{name} missing from .env.example"
