"""Per-project conversation sessions (v0.6).

Storage layout:
    <project_dir>/_collab/sessions/<session_uuid>.jsonl
    <project_dir>/_collab/sessions/<session_uuid>.jsonl.lock      (filelock sidecar)
    <project_dir>/_collab/file-history/<session_uuid>/<turn_id>/  (future: direct-tool snapshots)

Each .jsonl file is a stream of one JSON object per line. First line is the
session header (metadata); subsequent lines are turn entries — same schema
as v0.5's single-file workbench_session.jsonl, so the existing webview
renderer needs no changes for "read a session's history".

Metadata (last_active, message_count) is derived from file mtime + content
on each list call, so renames just touch the header and don't require a
sidecar metadata DB.

Migration: on first v0.6 access, if `_collab/workbench_session.jsonl` exists
and has content, it's moved into `_collab/sessions/<new-uuid>.jsonl` with a
synthesized header. The legacy file is removed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from filelock import FileLock
from pydantic import BaseModel


_SESSIONS_REL = "_collab/sessions"
_FILE_HISTORY_REL = "_collab/file-history"
_LEGACY_SESSION_REL = "_collab/workbench_session.jsonl"
_LOCK_TIMEOUT = 10  # seconds


class SessionMeta(BaseModel):
    session_id: str
    name: str
    created_at: str  # ISO 8601 UTC
    last_active: str  # ISO 8601 UTC
    message_count: int
    user_message_count: int
    forked_from: str | None = None


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _sessions_dir(project_dir: str) -> Path:
    return Path(project_dir) / _SESSIONS_REL


def _file_history_dir(project_dir: str) -> Path:
    return Path(project_dir) / _FILE_HISTORY_REL


def _session_path(project_dir: str, session_id: str) -> Path:
    return _sessions_dir(project_dir) / f"{session_id}.jsonl"


def _lock_path(session_path: Path) -> Path:
    return session_path.with_suffix(session_path.suffix + ".lock")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Migration: v0.5 single-session → first v0.6 session
# ---------------------------------------------------------------------------


def migrate_legacy_if_present(project_dir: str) -> str | None:
    """If a v0.5 workbench_session.jsonl exists, move it into the new
    sessions/ layout. Returns the new session_id, or None if nothing to do.
    """
    legacy = Path(project_dir) / _LEGACY_SESSION_REL
    if not legacy.exists() or legacy.stat().st_size == 0:
        return None
    session_id = str(uuid.uuid4())
    new_path = _session_path(project_dir, session_id)
    new_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_text = legacy.read_text(encoding="utf-8")
    header = {
        "type": "session_header",
        "session_id": session_id,
        "name": "Migrated from v0.5",
        "created_at": _now_iso(),
    }
    body = json.dumps(header) + "\n" + legacy_text
    if not body.endswith("\n"):
        body += "\n"
    with FileLock(str(_lock_path(new_path)), timeout=_LOCK_TIMEOUT):
        new_path.write_text(body, encoding="utf-8")
    # Remove the legacy file once we've safely written the new one.
    try:
        legacy.unlink()
    except OSError:
        pass
    return session_id


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def create_session(project_dir: str, name: str | None = None) -> str:
    """Create a new empty session with a header line. Returns session_id."""
    session_id = str(uuid.uuid4())
    path = _session_path(project_dir, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = _now_iso()
    header = {
        "type": "session_header",
        "session_id": session_id,
        "name": name or f"Session {now}",
        "created_at": now,
    }
    with FileLock(str(_lock_path(path)), timeout=_LOCK_TIMEOUT):
        path.write_text(json.dumps(header) + "\n", encoding="utf-8")
    return session_id


def list_sessions(project_dir: str) -> list[SessionMeta]:
    """List sessions sorted by last_active (most recent first)."""
    # Migrate before listing so a v0.5 user sees their old session.
    migrate_legacy_if_present(project_dir)
    d = _sessions_dir(project_dir)
    if not d.exists():
        return []
    out: list[SessionMeta] = []
    for p in sorted(d.glob("*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True):
        if p.suffix == ".lock":
            continue
        meta = _read_meta(p)
        if meta:
            out.append(meta)
    return out


def _read_meta(session_path: Path) -> SessionMeta | None:
    if not session_path.exists():
        return None
    try:
        lines = [
            line for line in session_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except OSError:
        return None
    header: dict[str, Any] = {}
    msg_count = 0
    user_msg_count = 0
    for line in lines:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "session_header":
            header = obj
            continue
        msg_count += 1
        if obj.get("role") == "user":
            user_msg_count += 1
    session_id = header.get("session_id") or session_path.stem
    name = header.get("name") or f"Session {session_id[:8]}"
    created_at = header.get("created_at") or datetime.fromtimestamp(
        session_path.stat().st_ctime, tz=timezone.utc
    ).isoformat(timespec="seconds")
    last_active = datetime.fromtimestamp(
        session_path.stat().st_mtime, tz=timezone.utc
    ).isoformat(timespec="seconds")
    return SessionMeta(
        session_id=session_id,
        name=name,
        created_at=created_at,
        last_active=last_active,
        message_count=msg_count,
        user_message_count=user_msg_count,
        forked_from=header.get("forked_from"),
    )


def read_session(project_dir: str, session_id: str) -> list[dict[str, Any]]:
    """Return all entries from a session including the header line."""
    path = _session_path(project_dir, session_id)
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        t = line.strip()
        if not t:
            continue
        try:
            entries.append(json.loads(t))
        except json.JSONDecodeError:
            continue
    return entries


def append_turn(
    project_dir: str, session_id: str, entries: list[dict[str, Any]]
) -> None:
    """Append one or more entries (typically a user message + assistant turn summary)."""
    path = _session_path(project_dir, session_id)
    if not path.exists():
        raise FileNotFoundError(f"session not found: {session_id}")
    with FileLock(str(_lock_path(path)), timeout=_LOCK_TIMEOUT):
        with path.open("a", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, default=str) + "\n")


def rename_session(project_dir: str, session_id: str, new_name: str) -> None:
    """Rewrite the header line with a new name. No-op if header is absent."""
    path = _session_path(project_dir, session_id)
    if not path.exists():
        raise FileNotFoundError(f"session not found: {session_id}")
    with FileLock(str(_lock_path(path)), timeout=_LOCK_TIMEOUT):
        lines = path.read_text(encoding="utf-8").splitlines()
        new_lines: list[str] = []
        replaced = False
        for line in lines:
            if not replaced and line.strip():
                try:
                    obj = json.loads(line)
                    if obj.get("type") == "session_header":
                        obj["name"] = new_name
                        new_lines.append(json.dumps(obj))
                        replaced = True
                        continue
                except json.JSONDecodeError:
                    pass
            new_lines.append(line)
        if not replaced:
            new_lines.insert(
                0,
                json.dumps(
                    {
                        "type": "session_header",
                        "session_id": session_id,
                        "name": new_name,
                        "created_at": _now_iso(),
                    }
                ),
            )
        path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def delete_session(project_dir: str, session_id: str) -> None:
    """Delete a session and its file-history snapshot dir (if any)."""
    path = _session_path(project_dir, session_id)
    if path.exists():
        path.unlink()
    lock = _lock_path(path)
    if lock.exists():
        try:
            lock.unlink()
        except OSError:
            pass
    fh = _file_history_dir(project_dir) / session_id
    if fh.exists():
        shutil.rmtree(fh, ignore_errors=True)


# ---------------------------------------------------------------------------
# Fork + Rewind
# ---------------------------------------------------------------------------


def fork_session(
    project_dir: str,
    source_id: str,
    pivot_turn_id: str | None = None,
    new_name: str | None = None,
) -> str:
    """Create a new session containing entries from source up to (and including)
    the pivot turn. pivot_turn_id=None forks the entire source.
    """
    src_entries = read_session(project_dir, source_id)
    if not src_entries:
        raise FileNotFoundError(f"source session has no entries: {source_id}")

    new_id = str(uuid.uuid4())
    now = _now_iso()
    src_header = next(
        (e for e in src_entries if e.get("type") == "session_header"), {}
    )
    base_name = src_header.get("name", f"Session {source_id[:8]}")
    fork_name = new_name or f"{base_name} (fork)"
    header = {
        "type": "session_header",
        "session_id": new_id,
        "name": fork_name,
        "created_at": now,
        "forked_from": source_id,
    }
    kept: list[dict[str, Any]] = [header]
    found_pivot = pivot_turn_id is None
    for e in src_entries:
        if e.get("type") == "session_header":
            continue
        kept.append(e)
        if pivot_turn_id is not None and e.get("turn_id") == pivot_turn_id:
            found_pivot = True
            break
    if not found_pivot:
        raise ValueError(f"pivot turn_id not found in source: {pivot_turn_id}")

    new_path = _session_path(project_dir, new_id)
    new_path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(_lock_path(new_path)), timeout=_LOCK_TIMEOUT):
        new_path.write_text(
            "\n".join(json.dumps(e, default=str) for e in kept) + "\n",
            encoding="utf-8",
        )
    return new_id


RewindMode = Literal["conversation", "code", "both"]


def rewind_session(
    project_dir: str,
    session_id: str,
    target_turn_id: str,
    mode: RewindMode = "both",
) -> dict[str, Any]:
    """Rewind a session to a target turn.

    mode = "conversation" → truncate JSONL after the target turn.
    mode = "code"         → `git reset --hard` to the turn's pre_execute_commit_hash.
    mode = "both"         → do both.

    Returns a status dict. If mode includes "code" but no pre_execute hash is
    present on the target turn (e.g. it was an analysis-only turn), git_reset_done
    will be False with a descriptive reason.
    """
    path = _session_path(project_dir, session_id)
    if not path.exists():
        raise FileNotFoundError(f"session not found: {session_id}")

    entries = read_session(project_dir, session_id)
    pre_hash: str | None = None
    kept: list[dict[str, Any]] = []
    found = False
    for e in entries:
        kept.append(e)
        if e.get("turn_id") == target_turn_id:
            found = True
            # Hash is on the assistant entry (the turn summary). The user entry
            # for this turn comes first and won't have it, so we keep walking
            # any same-turn entries that follow until the loop hits the boundary.
            if e.get("role") == "assistant" and e.get("pre_execute_commit_hash"):
                pre_hash = e["pre_execute_commit_hash"]
        elif found and e.get("turn_id") != target_turn_id:
            # We've passed the target turn — back the last entry out so kept
            # ends exactly at the last entry whose turn_id == target.
            kept.pop()
            break
    if not found:
        raise ValueError(f"turn not found: {target_turn_id}")
    # Collect pre_hash from any same-turn entry we kept (assistant lines).
    if pre_hash is None:
        for e in reversed(kept):
            if (
                e.get("turn_id") == target_turn_id
                and e.get("role") == "assistant"
                and e.get("pre_execute_commit_hash")
            ):
                pre_hash = e["pre_execute_commit_hash"]
                break

    git_reset_done = False
    git_error: str | None = None
    if mode in ("code", "both"):
        if pre_hash:
            try:
                subprocess.run(
                    ["git", "reset", "--hard", pre_hash],
                    cwd=project_dir,
                    check=True,
                    capture_output=True,
                )
                git_reset_done = True
            except subprocess.CalledProcessError as e:
                git_error = e.stderr.decode("utf-8", errors="replace")[:300]
        else:
            git_error = "no pre_execute_commit_hash on target turn"

    if mode in ("conversation", "both"):
        with FileLock(str(_lock_path(path)), timeout=_LOCK_TIMEOUT):
            path.write_text(
                "\n".join(json.dumps(e, default=str) for e in kept) + "\n",
                encoding="utf-8",
            )

    return {
        "rewound": True,
        "mode": mode,
        "git_reset_done": git_reset_done,
        "git_error": git_error,
        "kept_messages": len(kept),
    }
