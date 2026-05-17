"""cothink HTTP+SSE bridge — consumed by the VSCode extension.

This is a thin FastAPI layer over the existing cothink Python code. No business
logic lives here — every endpoint just wraps the LangGraph pipeline, the chat
debate loop, or the memory module and streams events out as Server-Sent Events
where appropriate.

Designed to bind to 127.0.0.1 only (localhost). The extension launches this as
a child process on activation. No auth — the localhost binding IS the security
boundary for a single-user dev tool.

Endpoints:
    GET  /health   → model identifiers + liveness
    POST /build    → SSE; runs 5-node integrity pipeline, streams node_complete events
    POST /chat     → SSE; runs debate-mode loop, streams round_chunk + round_complete
    GET  /memory   → JSON; returns _collab/LEARNINGS.md contents + entry count
    POST /memory   → JSON; appends a LearningEntry to _collab/LEARNINGS.md
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from .chat import (
    DEBATER_SYSTEM,
    STOP_PHRASE,
    _build_debate_prompt,
    _is_stop_signal,
)
from .graph import build_graph
from .memory import append_learning, load_learnings
from . import sessions as session_store
from .image_handler import decode_and_save
from .nodes import (
    CLAUDE_MODEL,
    GEMINI_FLASH_MODEL,
    GEMINI_MODEL,
    _claude_stream,
    _gemini_stream,
    _git_is_dirty,
    chunk_emitter_var,
)
from .state import CothinkState, LearningEntry


# Mirror cli.py's package-root resolution for .env discovery.
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# Request/response schemas
# ---------------------------------------------------------------------------


class ImagePayload(BaseModel):
    """v0.6.2 — one pasted screenshot. base64-encoded; downsampled server-side."""
    filename: str = ""
    data_base64: str


class MentionPayload(BaseModel):
    """v0.6.6 — one @-mention chip from the composer.

    `kind` is "file" | "folder" | "symbol". `path` is workspace-relative.
    Discovery pre-reads file mentions and folds their content into user_msg
    so Claude/Gemini don't have to discover via tools when the user already
    pointed at the right place.
    """
    kind: str
    path: str
    # Optional symbol pinned within a file (e.g., "function_name") when
    # kind == "symbol". Lets Discovery grep + extract the symbol's lines
    # rather than dumping the whole file.
    symbol: str = ""


class BuildRequest(BaseModel):
    task: str
    project_dir: str
    session_id: str | None = None  # v0.6: append turn into this session if set
    images: list[ImagePayload] = Field(default_factory=list)  # v0.6.2
    mentions: list[MentionPayload] = Field(default_factory=list)  # v0.6.6


class ChatRequest(BaseModel):
    message: str
    history: list[dict[str, Any]] = Field(default_factory=list)
    project_dir: str = "."


class MemoryAppendRequest(BaseModel):
    project_dir: str
    component: str
    decision: str
    rule: str


# v0.6 session endpoints
class SessionCreateRequest(BaseModel):
    project_dir: str
    name: str | None = None


class SessionRenameRequest(BaseModel):
    project_dir: str
    name: str


class SessionForkRequest(BaseModel):
    project_dir: str
    pivot_turn_id: str | None = None
    new_name: str | None = None


class SessionRewindRequest(BaseModel):
    project_dir: str
    target_turn_id: str
    mode: str = "both"  # "code" | "conversation" | "both"


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _create_app() -> FastAPI:
    app = FastAPI(title="cothink", version="0.5.0")

    # VSCode webviews use vscode-webview:// URIs which won't appear as a
    # standard CORS origin. Server is bound to 127.0.0.1 only — the host
    # binding IS the security boundary for this single-user dev tool, so
    # we allow all origins here.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "claude_model": CLAUDE_MODEL,
            "gemini_model": GEMINI_MODEL,
            "flash_model": GEMINI_FLASH_MODEL,
        }

    @app.get("/memory")
    async def get_memory(project_dir: str) -> dict[str, Any]:
        markdown, count = load_learnings(project_dir)
        return {"markdown": markdown, "count": count}

    @app.post("/memory")
    async def post_memory(req: MemoryAppendRequest) -> dict[str, Any]:
        entry = LearningEntry(
            component=req.component,
            decision=req.decision,
            rule=req.rule,
            run_id=str(uuid.uuid4())[:8],
            timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        append_learning(req.project_dir, entry)
        return {"ok": True}

    @app.post("/build")
    async def build(req: BuildRequest) -> EventSourceResponse:
        resolved = str(Path(req.project_dir).resolve())
        if not Path(resolved).exists():
            raise HTTPException(status_code=400, detail=f"project_dir not found: {resolved}")
        is_dirty, status = _git_is_dirty(resolved)
        if is_dirty:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Working tree at {resolved} has uncommitted changes. "
                    f"Commit or stash before running cothink build."
                ),
            )
        # v0.6.2: convert ImagePayload models to plain dicts for the handler.
        # Decoding + downsampling happens inside _build_event_stream so the
        # generated turn_id can be used in the filename.
        image_dicts = [img.model_dump() for img in req.images] if req.images else []
        # v0.6.6 Context Chips: pass mentions through. Discovery pre-reads
        # file mentions and folds them into the user_msg.
        mention_dicts = [m.model_dump() for m in req.mentions] if req.mentions else []
        return EventSourceResponse(
            _build_event_stream(
                req.task, resolved, req.session_id, image_dicts, mention_dicts
            )
        )

    @app.post("/chat")
    async def chat(req: ChatRequest) -> EventSourceResponse:
        resolved = str(Path(req.project_dir).resolve())
        if not Path(resolved).exists():
            raise HTTPException(status_code=400, detail=f"project_dir not found: {resolved}")
        return EventSourceResponse(_chat_event_stream(req.message, req.history, resolved))

    # ------------------------------------------------------------------
    # v0.6 session endpoints
    # ------------------------------------------------------------------

    @app.get("/sessions")
    async def list_sessions(project_dir: str) -> dict[str, Any]:
        resolved = str(Path(project_dir).resolve())
        if not Path(resolved).exists():
            raise HTTPException(status_code=400, detail=f"project_dir not found: {resolved}")
        metas = session_store.list_sessions(resolved)
        return {"sessions": [m.model_dump() for m in metas]}

    @app.post("/sessions")
    async def create_session(req: SessionCreateRequest) -> dict[str, Any]:
        resolved = str(Path(req.project_dir).resolve())
        if not Path(resolved).exists():
            raise HTTPException(status_code=400, detail=f"project_dir not found: {resolved}")
        sid = session_store.create_session(resolved, req.name)
        return {"session_id": sid}

    @app.get("/sessions/{session_id}")
    async def get_session(session_id: str, project_dir: str) -> dict[str, Any]:
        resolved = str(Path(project_dir).resolve())
        if not Path(resolved).exists():
            raise HTTPException(status_code=400, detail=f"project_dir not found: {resolved}")
        entries = session_store.read_session(resolved, session_id)
        if not entries:
            raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
        return {"entries": entries}

    @app.patch("/sessions/{session_id}")
    async def rename_session(session_id: str, req: SessionRenameRequest) -> dict[str, Any]:
        resolved = str(Path(req.project_dir).resolve())
        if not Path(resolved).exists():
            raise HTTPException(status_code=400, detail=f"project_dir not found: {resolved}")
        try:
            session_store.rename_session(resolved, session_id, req.name)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        return {"ok": True}

    @app.delete("/sessions/{session_id}")
    async def delete_session(session_id: str, project_dir: str) -> dict[str, Any]:
        resolved = str(Path(project_dir).resolve())
        if not Path(resolved).exists():
            raise HTTPException(status_code=400, detail=f"project_dir not found: {resolved}")
        session_store.delete_session(resolved, session_id)
        return {"ok": True}

    @app.post("/sessions/{session_id}/fork")
    async def fork_session(session_id: str, req: SessionForkRequest) -> dict[str, Any]:
        resolved = str(Path(req.project_dir).resolve())
        if not Path(resolved).exists():
            raise HTTPException(status_code=400, detail=f"project_dir not found: {resolved}")
        try:
            new_id = session_store.fork_session(
                resolved, session_id, req.pivot_turn_id, req.new_name
            )
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"session_id": new_id}

    @app.post("/sessions/{session_id}/rewind")
    async def rewind_session(session_id: str, req: SessionRewindRequest) -> dict[str, Any]:
        resolved = str(Path(req.project_dir).resolve())
        if not Path(resolved).exists():
            raise HTTPException(status_code=400, detail=f"project_dir not found: {resolved}")
        if req.mode not in ("code", "conversation", "both"):
            raise HTTPException(
                status_code=400,
                detail=f"mode must be one of: code, conversation, both (got: {req.mode})",
            )
        try:
            result = session_store.rewind_session(
                resolved, session_id, req.target_turn_id, req.mode  # type: ignore[arg-type]
            )
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return result

    return app


# ---------------------------------------------------------------------------
# /build SSE stream — wraps graph.astream
# ---------------------------------------------------------------------------


async def _build_event_stream(
    task: str,
    project_dir: str,
    session_id: str | None = None,
    images: list[dict[str, str]] | None = None,
    mentions: list[dict] | None = None,
):
    """Async generator yielding SSE events for one build run.

    Emits three kinds of events:
      - thinking_chunk: a fragment of streamed reasoning from one of the LLMs,
        emitted live as Claude or Gemini generates. Payload:
        {node, speaker, role, text}.
      - node_complete: emitted when a LangGraph node finishes. Payload:
        {node_name, log, dialogue_delta, halt_reason?}.
      - error: emitted on any unhandled exception in the graph or stream.

    Final event is `done` with the resolved final state summary.

    Implementation: ONE async queue carries all event types as
    (kind, payload) tuples. Nodes push chunks via the `chunk_emitter_var`
    contextvar; the graph runner pushes node_complete + a final sentinel.
    The generator drains the queue serially — no cancellation races, no
    asyncio.wait branches, no lost chunks.
    """
    graph = build_graph()
    # v0.6: server-owned turn_id so the session JSONL and the client share
    # a single stable identifier per turn. Emitted in the initial event so
    # the webview can use it as its DOM key from the start.
    turn_id = "t" + uuid.uuid4().hex[:12]
    yield {
        "event": "turn_id",
        "data": json.dumps({"turn_id": turn_id}),
    }

    # v0.6.2: decode + downsample any pasted images BEFORE the graph starts.
    # The Pillow downsample (≤1024px max dim) is the locked safeguard from
    # the Gemini debate — keeps the symmetric dual-LLM critique from
    # accelerating the 429 cliff on full-res screenshots.
    saved_image_paths: list[str] = []
    if images:
        try:
            saved_image_paths = decode_and_save(project_dir, turn_id, images)
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(
                f"cothink: image decode failed (turn {turn_id}): "
                f"{type(e).__name__}: {e}\n"
            )
        if saved_image_paths:
            yield {
                "event": "images_attached",
                "data": json.dumps(
                    {"turn_id": turn_id, "paths": saved_image_paths}
                ),
            }

    initial = CothinkState(
        user_request=task,
        project_dir=project_dir,
        attached_images=saved_image_paths,
        attached_mentions=mentions or [],
    )
    config = {
        "configurable": {"thread_id": str(uuid.uuid4())},
        "recursion_limit": 50,
    }
    seen_dialogue = 0
    final_state: dict[str, Any] = {}

    event_queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    def emit_chunk(payload: dict[str, Any]) -> None:
        event_queue.put_nowait(("thinking_chunk", payload))

    chunk_emitter_var.set(emit_chunk)

    async def run_graph() -> None:
        nonlocal seen_dialogue
        try:
            async for event in graph.astream(initial, config=config):
                for node_name, node_state in event.items():
                    payload: dict[str, Any] = {
                        "node_name": node_name,
                        "log": None,
                        "dialogue_delta": [],
                        "halt_reason": None,
                    }
                    if isinstance(node_state, dict):
                        final_state.update(node_state)
                        log_lines = node_state.get("log") or []
                        if log_lines:
                            payload["log"] = log_lines[-1]
                        dialogue = node_state.get("dialogue") or []
                        new_entries = dialogue[seen_dialogue:]
                        payload["dialogue_delta"] = [
                            e.model_dump() if hasattr(e, "model_dump") else e
                            for e in new_entries
                        ]
                        seen_dialogue = len(dialogue)
                        halt = node_state.get("halt_reason")
                        if halt:
                            payload["halt_reason"] = halt
                    await event_queue.put(("node_complete", payload))
        except Exception as e:  # noqa: BLE001
            await event_queue.put(
                ("error", {"reason": f"{type(e).__name__}: {e}"})
            )
        finally:
            await event_queue.put(("__done__", None))

    graph_task = asyncio.create_task(run_graph())

    try:
        while True:
            kind, payload = await event_queue.get()
            if kind == "__done__":
                break
            yield {"event": kind, "data": json.dumps(payload, default=str)}
    except Exception as e:  # noqa: BLE001
        graph_task.cancel()
        yield {
            "event": "error",
            "data": json.dumps({"reason": f"stream: {type(e).__name__}: {e}"}),
        }
        return

    # Serialize proposed_diffs (list of ProposedDiff pydantic models or dicts)
    # into a lightweight {file_path, content_preview} payload for the build
    # panel's "files written" list. Full content is on disk; we just need the
    # paths to open via vscode.workspace.openTextDocument.
    proposed_diffs_payload: list[dict] = []
    for d in final_state.get("proposed_diffs") or []:
        item = d.model_dump() if hasattr(d, "model_dump") else dict(d)
        proposed_diffs_payload.append(
            {
                "file_path": item.get("file_path"),
                "contract_bullet_quoted": item.get("contract_bullet_quoted", "")[:200],
            }
        )

    done_payload = {
        "turn_id": turn_id,
        "halt_reason": final_state.get("halt_reason"),
        "pre_execute_commit_hash": final_state.get("pre_execute_commit_hash"),
        "proposed_diffs": proposed_diffs_payload,
        "design_contract": final_state.get("design_contract") or [],
    }

    # v0.6: persist the turn into the session JSONL if a session_id was
    # supplied. Append both the user message and the assistant summary so
    # subsequent reads can render the full conversation faithfully. Persist
    # errors don't break the response — the stream still ends cleanly.
    if session_id:
        try:
            session_store.append_turn(
                project_dir,
                session_id,
                [
                    {"role": "user", "content": task, "turn_id": turn_id},
                    {"role": "assistant", **done_payload},
                ],
            )
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(
                f"cothink: failed to append turn to session {session_id}: "
                f"{type(e).__name__}: {e}\n"
            )

    yield {
        "event": "done",
        "data": json.dumps(done_payload, default=str),
    }


# ---------------------------------------------------------------------------
# /chat SSE stream — debate-mode loop, no rich/Live dependency
# ---------------------------------------------------------------------------


async def _chat_event_stream(message: str, history: list[dict[str, Any]], project_dir: str):
    """Async generator yielding SSE events for one chat turn (one user message,
    possibly many debate rounds).

    Per round, emits:
      - round_start  {round, speaker}
      - round_chunk  {round, speaker, text}   (one per streamed text chunk)
      - round_complete {round, speaker, full_text, is_stop_signal}
    Final event: `done` with the compressed history summary the client should
    use for its next request.
    """
    # Local working copy — we don't mutate the client's history.
    working_history: list[dict[str, Any]] = list(history)
    working_history.append({"role": "user", "content": message})
    debate_start = len(working_history)

    round_num = 0
    try:
        while True:
            round_num += 1
            is_claude = round_num % 2 == 1
            speaker = "claude" if is_claude else "gemini"
            stream_fn = _claude_stream if is_claude else _gemini_stream

            yield {
                "event": "round_start",
                "data": json.dumps({"round": round_num, "speaker": speaker}),
            }

            user_prompt = _build_debate_prompt(working_history)
            chunks: list[str] = []
            async for chunk in stream_fn(DEBATER_SYSTEM, user_prompt):
                chunks.append(chunk)
                yield {
                    "event": "round_chunk",
                    "data": json.dumps(
                        {"round": round_num, "speaker": speaker, "text": chunk}
                    ),
                }

            full_text = "".join(chunks).strip()
            is_stop = _is_stop_signal(full_text)
            yield {
                "event": "round_complete",
                "data": json.dumps(
                    {
                        "round": round_num,
                        "speaker": speaker,
                        "full_text": full_text,
                        "is_stop_signal": is_stop,
                    }
                ),
            }

            if is_stop:
                working_history.append(
                    {
                        "role": "assistant",
                        "speaker": speaker,
                        "round": round_num,
                        "content": STOP_PHRASE,
                    }
                )
                break

            working_history.append(
                {
                    "role": "assistant",
                    "speaker": speaker,
                    "round": round_num,
                    "content": full_text,
                }
            )
    except Exception as e:  # noqa: BLE001
        yield {
            "event": "error",
            "data": json.dumps({"reason": f"{type(e).__name__}: {e}"}),
        }
        return

    compressed_history = _compress_history_inline(working_history, debate_start)

    yield {
        "event": "done",
        "data": json.dumps(
            {"compressed_history": compressed_history, "total_rounds": round_num}
        ),
    }


def _compress_history_inline(
    history: list[dict[str, Any]], debate_start: int
) -> list[dict[str, Any]]:
    """Same context-rot mitigation as cothink.chat._compress_debate_history,
    but without the rich.Console print side-effect (server has no console).
    """
    debate_entries = history[debate_start:]
    if not debate_entries:
        return history
    substantive = [
        e for e in debate_entries
        if e.get("content") and e["content"] != STOP_PHRASE
    ]
    if substantive:
        final = substantive[-1]
        summary = {
            "role": "assistant",
            "speaker": "team",
            "round": 0,
            "content": (
                f"[debate ended after {len(debate_entries)} rounds; "
                f"final substantive contribution by {final.get('speaker', '?').upper()}:]\n"
                f"{final['content']}"
            ),
        }
        return history[:debate_start] + [summary]
    return history[:debate_start] + [
        {
            "role": "assistant",
            "speaker": "team",
            "round": 0,
            "content": "(debate yielded no substantive content)",
        }
    ]


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def _load_env() -> None:
    env_path = _PACKAGE_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path)
    else:
        load_dotenv()
    # Mirror cli.py: force Claude calls through claude-agent-sdk's subscription auth.
    os.environ.pop("ANTHROPIC_API_KEY", None)
    if not os.environ.get("GEMINI_API_KEY"):
        sys.stderr.write(
            "cothink server: missing env var GEMINI_API_KEY. "
            "Set it in cothink/.env before starting the server.\n"
        )
        sys.exit(2)


def run_server(host: str = "127.0.0.1", port: int = 8765) -> None:
    """Programmatic entry point — also used by `cothink --serve` in cli.py."""
    _load_env()
    app = _create_app()
    uvicorn.run(app, host=host, port=port, log_level="info")


def main() -> None:
    """Console-script entry point — `cothink-serve` runs this."""
    import argparse

    parser = argparse.ArgumentParser(prog="cothink-serve")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    run_server(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
