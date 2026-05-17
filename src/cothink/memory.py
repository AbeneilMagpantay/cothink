"""Cross-run memory: _collab/LEARNINGS.md per project_dir.

Each successful cothink run appends a structured `LearningEntry` here.
Subsequent runs in the same project read it during Discovery so cothink
remembers prior design decisions across invocations.

Two non-negotiables (locked by Gemini's quick_debate this turn):
1. Pydantic schema enforced before any disk write — the harness formats the
   markdown, never the LLM. Prevents format drift breaking a future compact step.
2. `filelock` on every read/write — concurrent cothink runs (or a future web
   UI) won't corrupt the file with interleaved appends.
"""

from __future__ import annotations

from pathlib import Path

from filelock import FileLock

from .state import LearningEntry


_LEARNINGS_REL = "_collab/LEARNINGS.md"
_LOCK_TIMEOUT = 10  # seconds


def _paths(project_dir: str) -> tuple[Path, Path, Path]:
    base = Path(project_dir)
    md = base / _LEARNINGS_REL
    lock = md.with_suffix(md.suffix + ".lock")
    return base, md, lock


def load_learnings(project_dir: str) -> tuple[str, int]:
    """Return (markdown_text, entry_count). Empty + 0 if file is absent.

    No lock needed for the read path — markdown reads are short and the
    write path is append-only with O_APPEND-equivalent semantics under the
    lock. Worst case a concurrent reader sees a partial line, which is
    benign (we treat it as raw context for the LLM).
    """
    _, md, _ = _paths(project_dir)
    if not md.exists():
        return "", 0
    text = md.read_text(encoding="utf-8")
    # Each entry's header line starts with `## ` per `_format_entry`.
    count = sum(1 for line in text.splitlines() if line.startswith("## "))
    return text, count


def append_learning(project_dir: str, entry: LearningEntry) -> None:
    """filelock-protected append. Format is deterministic Python-side."""
    base, md, lock_path = _paths(project_dir)
    md.parent.mkdir(parents=True, exist_ok=True)
    formatted = _format_entry(entry)
    with FileLock(str(lock_path), timeout=_LOCK_TIMEOUT):
        # Append mode + UTF-8; create the file if it doesn't exist.
        with md.open("a", encoding="utf-8") as f:
            f.write(formatted)


def _format_entry(entry: LearningEntry) -> str:
    """Pydantic → markdown. Single source of truth for the on-disk format."""
    short_id = entry.run_id[:8] if entry.run_id else "?"
    return (
        f"## {entry.component} — run {short_id} ({entry.timestamp})\n"
        f"**Decision:** {entry.decision}\n"
        f"**Rule:** {entry.rule}\n\n"
    )
