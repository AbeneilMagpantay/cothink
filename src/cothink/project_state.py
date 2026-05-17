"""v0.6.5 — Project State Journal.

Solves fragmented-Q&A FACT DRIFT (the dominant cothink failure mode per
all 3 forensic profiles + DR #3 Section 12). Distinct from:

  - `LEARNINGS.md` — captures RULES ("never use date.today() on Cloud Run")
  - `sessions/<uuid>.jsonl` — the full noisy transcript
  - `AGENTS.md` (Crush-style) — static project conventions

`project_state.md` captures STATE-OF-THE-WORLD facts: what's pending,
what was DRAFTED but not SENT, what's confirmed with a source, what's
blocked on stakeholder input. Auto-updated at END OF TURN by a single
Gemini Pro call (`project_state_node`) after Contract Review APPROVE.
Locked design (Gemini debate, 2026-05-16 confidence 0.95): NEVER updated
during Planning — Planning is intent, not reality.

Storage: `<project_dir>/_collab/project_state.md`.
Lock pattern mirrors `memory.py` (filelock sidecar). Pydantic owns the
on-disk markdown format — the LLM emits structured state, the harness
renders. Prevents format drift the LLM might introduce.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock

from .state import ProjectStateUpdate


_JOURNAL_REL = "_collab/project_state.md"
_LOCK_TIMEOUT = 10  # seconds


def _paths(project_dir: str) -> tuple[Path, Path]:
    base = Path(project_dir)
    md = base / _JOURNAL_REL
    lock = md.with_suffix(md.suffix + ".lock")
    return md, lock


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def load_journal(project_dir: str) -> str:
    """Return the current journal markdown, or empty string if absent.

    Read path is lock-free — markdown reads are short and tolerate a
    partially-written file (the writer atomically replaces; partials only
    visible during the brief replace window).
    """
    md, _ = _paths(project_dir)
    if not md.exists():
        return ""
    try:
        return md.read_text(encoding="utf-8")
    except OSError:
        return ""


def load_journal_for_prompt(project_dir: str, max_chars: int = 4000) -> str:
    """Return the journal wrapped as a PROJECT STATE block ready to prepend
    to the Discovery prompt. Empty string if no journal exists yet.

    Capped to avoid bloating context. If the journal grows past max_chars,
    keep the head (sections + latest summary) and truncate the middle.
    """
    raw = load_journal(project_dir)
    if not raw.strip():
        return ""
    if len(raw) > max_chars:
        # Keep first half + last 1KB (most recent turn_summary lives there)
        keep_head = max_chars - 1024
        raw = raw[:keep_head] + "\n\n…(truncated; see full file at _collab/project_state.md)…\n\n" + raw[-1024:]
    return (
        "=== PROJECT STATE (authoritative; trust over your own recall) ===\n"
        f"{raw}\n"
        "=== END PROJECT STATE ===\n\n"
    )


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


def save_journal(project_dir: str, update: ProjectStateUpdate, turn_id: str) -> None:
    """Render the structured update as markdown and write it (replacing the
    whole file — the LLM emits FULL CURRENT STATE every turn, not deltas).

    Atomic via the standard `os.replace`-after-write pattern; filelock
    serializes concurrent writers (e.g. two cothink runs on the same
    project).
    """
    md, lock_path = _paths(project_dir)
    md.parent.mkdir(parents=True, exist_ok=True)
    rendered = _render(update, turn_id=turn_id, timestamp=_now_iso())
    tmp = md.with_suffix(md.suffix + ".tmp")
    with FileLock(str(lock_path), timeout=_LOCK_TIMEOUT):
        tmp.write_text(rendered, encoding="utf-8")
        tmp.replace(md)


def _render(update: ProjectStateUpdate, *, turn_id: str, timestamp: str) -> str:
    """Pydantic → markdown. Single source of truth for the on-disk format.

    Format chosen so the LLM can re-read its own output verbatim on the
    next turn and understand the structure without re-derivation.
    """
    lines: list[str] = [
        "# Project State",
        "",
        "_Auto-maintained by cothink at end of every approved turn. Treat as authoritative; the AI must defer to this file over its own recall for state-of-the-world facts._",
        "",
        "## Currently pending",
    ]
    if update.pending:
        for item in update.pending:
            lines.append(f"- {item}")
    else:
        lines.append("- (none)")
    lines.append("")

    lines.append("## Drafted but NOT sent")
    if update.drafted_not_sent:
        for item in update.drafted_not_sent:
            lines.append(f"- {item}")
    else:
        lines.append("- (none)")
    lines.append("")

    lines.append("## Confirmed facts")
    if update.confirmed_facts:
        for item in update.confirmed_facts:
            lines.append(f"- {item}")
    else:
        lines.append("- (none)")
    lines.append("")

    lines.append("## Open questions (need stakeholder input)")
    if update.open_questions:
        for item in update.open_questions:
            lines.append(f"- {item}")
    else:
        lines.append("- (none)")
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(f"**Last updated:** {timestamp} · turn `{turn_id}`")
    lines.append("")
    if update.turn_summary:
        lines.append(f"**This turn:** {update.turn_summary}")
        lines.append("")

    return "\n".join(lines)
