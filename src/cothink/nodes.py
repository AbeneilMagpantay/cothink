"""Five nodes plus Human Fallback.

Each node receives the full CothinkState and returns a dict of state mutations.
Claude calls go through claude-agent-sdk (uses Claude Code subscription auth, not API billing).
Gemini calls go through google-genai SDK directly with GEMINI_API_KEY.
"""

import contextvars
import json
import os
import py_compile
import re
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from typing import Any

from claude_agent_sdk import (
    query as claude_query,
    ClaudeAgentOptions,
    AssistantMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    ToolResultBlock,
    UserMessage,
)
from google import genai
from google.genai import types as gtypes

from .memory import append_learning, load_learnings
from .prompts import (
    DISCOVERY_SYSTEM_BOTH,
    DISCOVERY_SYSTEM_CLAUDE_TOOLED,
    PLANNING_SYSTEM_BOTH,
    PLANNING_VERDICT_GEMINI,
    EXECUTING_SYSTEM_CLAUDE,
    EXECUTING_VERDICT_GEMINI,
    CONTRACT_REVIEW_SYSTEM_GEMINI,
    PROJECT_STATE_UPDATER_GEMINI,
)
from . import project_state as project_state_journal
from .state import (
    CothinkState,
    DialogueEntry,
    GeminiVerdict,
    Invariant,
    ProjectStateUpdate,
    LearningEntry,
    ProposedDiff,
)


CLAUDE_MODEL = os.environ.get("COTHINK_CLAUDE_MODEL", "claude-opus-4-7")
GEMINI_MODEL = os.environ.get("COTHINK_GEMINI_MODEL", "gemini-3.1-pro-preview")
GEMINI_FLASH_MODEL = os.environ.get(
    "COTHINK_GEMINI_FLASH_MODEL", "gemini-3.1-flash-lite"
)


_gemini_singleton: genai.Client | None = None


# v0.5.4-streaming: per-/build-request chunk emitter. The server installs a
# callable here (via contextvar) that pushes streamed token chunks to an
# asyncio.Queue, which the SSE generator drains and re-emits as
# `thinking_chunk` events. Nodes get live token streaming "for free" — they
# call _claude_call_streaming / _gemini_call_streaming and chunks flow
# through to the webview without each node knowing about SSE.
ChunkEmitter = Callable[[dict], None]
chunk_emitter_var: contextvars.ContextVar[Optional[ChunkEmitter]] = (
    contextvars.ContextVar("cothink_chunk_emitter", default=None)
)


def _emit_chunk(node: str, speaker: str, role: str, text: str) -> None:
    """Best-effort chunk emit. No-op if no emitter is installed (CLI runs)."""
    emit = chunk_emitter_var.get()
    if emit is None or not text:
        return
    try:
        emit({"node": node, "speaker": speaker, "role": role, "text": text})
    except Exception:
        # Never let a streaming hiccup take down a node call.
        pass


def _gemini() -> genai.Client:
    """Module-level singleton genai client.

    LangGraph wraps each node in asyncio.create_task and the task scope
    cleans up resources held inside it — including google-genai's internal
    httpx client if it was created within that task. A module-level
    singleton lives outside any task scope and survives across node calls.
    """
    global _gemini_singleton
    if _gemini_singleton is None:
        _gemini_singleton = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return _gemini_singleton


async def _claude_call(
    system: str,
    user: str,
    *,
    allowed_tools: list[str] | None = None,
    max_turns: int = 1,
    cwd: str | None = None,
) -> str:
    """Call Claude via claude-agent-sdk (subscription-billed, not API).

    Default (no kwargs): pure text-completion. `allowed_tools=[]`, single turn,
    no file access. The SDK uses Claude Code's auth — no ANTHROPIC_API_KEY needed.

    With kwargs: enables tool-using agentic loops (Read/Glob/Grep/etc.). The SDK
    handles the tool-call → tool-result → continue cycle internally; we just
    accumulate the TextBlock content from every AssistantMessage in the stream
    (narration plus the final summary). When `allowed_tools` is non-empty:
      - permission_mode='bypassPermissions' so Claude auto-executes tools
      - setting_sources=[] to avoid inheriting the user's Claude Code settings
      - cwd should be set to scope file ops to the project directory
    """
    captured_stderr: list[str] = []
    opts_kwargs: dict = {
        "system_prompt": system,
        "allowed_tools": allowed_tools or [],
        "model": CLAUDE_MODEL,
        "max_turns": max_turns,
        "stderr": lambda line: captured_stderr.append(line),
    }
    if allowed_tools:
        opts_kwargs["permission_mode"] = "bypassPermissions"
        opts_kwargs["setting_sources"] = []
    if cwd:
        opts_kwargs["cwd"] = cwd

    options = ClaudeAgentOptions(**opts_kwargs)
    chunks: list[str] = []
    try:
        async for message in claude_query(prompt=user, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        chunks.append(block.text)
    except Exception as e:
        details = "\n".join(captured_stderr[-30:]) or "(no stderr captured)"
        raise RuntimeError(
            f"Claude call failed: {type(e).__name__}: {e}\n"
            f"system_prompt_len={len(system)}, user_prompt_len={len(user)}, "
            f"tools={allowed_tools or []}, max_turns={max_turns}\n"
            f"--- last claude stderr ---\n{details}"
        ) from e
    return "".join(chunks)


async def _gemini_call(
    system: str,
    user: str,
    response_schema: type | None = None,
    *,
    use_pro: bool = True,
) -> str:
    """Call Gemini via google-genai's sync API directly inside the async fn.

    Uses the module-level singleton client (see `_gemini`). asyncio.to_thread
    is intentionally avoided because LangGraph's task wrapping caused the
    spawned thread to see a closed httpx client. A direct sync call blocks
    the loop briefly per node call (acceptable for ≤10 calls per task).

    v0.5.4: Flash routing removed. Every Gemini call uses Gemini 3.1 Pro —
    the dual-brain integrity value of two top-tier reasoners on every step
    is the whole point of cothink. `use_pro` kept as a parameter for
    backwards-compat with existing callers; the value is ignored.
    """
    del use_pro  # always Pro now
    model = GEMINI_MODEL
    config = gtypes.GenerateContentConfig(system_instruction=system)
    if response_schema is not None:
        config.response_mime_type = "application/json"
        config.response_schema = response_schema

    resp = _gemini().models.generate_content(
        model=model,
        contents=user,
        config=config,
    )
    return resp.text


# ---------------------------------------------------------------------------
# Streaming variants — used by chat mode for live rich.Live rendering.
# Non-streaming versions above stay for build-mode nodes that need full text
# for Pydantic verdict parsing.
# ---------------------------------------------------------------------------
from typing import AsyncIterator


# v0.6.2 — multimodal prompt builders for image-bearing turns.

import base64 as _base64
from .image_handler import load_image_bytes as _load_image_bytes


def _build_claude_multimodal_prompt(text: str, image_paths: list[str]):
    """Return an async iterable of one user message dict per claude-agent-sdk
    streaming-mode contract. Each image is encoded inline as a base64 image
    content block alongside the text.
    """
    content_blocks: list[dict] = [{"type": "text", "text": text}]
    for path in image_paths:
        try:
            data, mime = _load_image_bytes(path)
        except Exception:
            continue
        content_blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": mime,
                "data": _base64.b64encode(data).decode("ascii"),
            },
        })

    msg = {
        "type": "user",
        "message": {"role": "user", "content": content_blocks},
        "parent_tool_use_id": None,
        "session_id": uuid.uuid4().hex,
    }

    async def _iter():
        yield msg
    return _iter()


def _build_gemini_multimodal_contents(text: str, image_paths: list[str]) -> list:
    """Return a list of google-genai Parts mixing text + image bytes."""
    parts = [gtypes.Part.from_text(text=text)]
    for path in image_paths:
        try:
            data, mime = _load_image_bytes(path)
        except Exception:
            continue
        parts.append(gtypes.Part.from_bytes(data=data, mime_type=mime))
    return parts


async def _claude_stream(
    system: str, user: str, *, image_paths: list[str] | None = None
) -> AsyncIterator[str]:
    """Yield text chunks from Claude as they arrive.

    v0.6.2: optional `image_paths` switches to the streaming-mode dict prompt
    so the images ride alongside the text as Anthropic-format content blocks.
    """
    options = ClaudeAgentOptions(
        system_prompt=system,
        allowed_tools=[],
        model=CLAUDE_MODEL,
        max_turns=1,
    )
    if image_paths:
        prompt_arg = _build_claude_multimodal_prompt(user, image_paths)
    else:
        prompt_arg = user
    async for message in claude_query(prompt=prompt_arg, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    yield block.text


async def _gemini_stream(
    system: str,
    user: str,
    *,
    use_pro: bool = True,
    image_paths: list[str] | None = None,
) -> AsyncIterator[str]:
    """Yield text chunks from Gemini's generate_content_stream API.

    v0.5.4: always Gemini 3.1 Pro (locked). `use_pro` is a no-op kwarg.
    v0.6.2: optional `image_paths` switches `contents` from a string to a
    list of mixed-modality Parts.
    """
    del use_pro
    model = GEMINI_MODEL
    config = gtypes.GenerateContentConfig(system_instruction=system)
    if image_paths:
        contents = _build_gemini_multimodal_contents(user, image_paths)
    else:
        contents = user
    for chunk in _gemini().models.generate_content_stream(
        model=model, contents=contents, config=config
    ):
        text = getattr(chunk, "text", None)
        if text:
            yield text


# ---------------------------------------------------------------------------
# v0.5.4-streaming: build-pipeline streaming helpers.
#
# These wrap _claude_stream / _gemini_stream:
#   - emit each chunk through the contextvar emitter (→ SSE thinking_chunk)
#   - collect chunks into one full-text string for downstream Pydantic parsing
#   - same return shape as _claude_call / _gemini_call (str), drop-in usable
#
# Use these in nodes whenever the call is NOT response_schema-bound.
# Schema-bound verdict calls (PLANNING_VERDICT_GEMINI / EXECUTING_VERDICT_GEMINI /
# CONTRACT_REVIEW_SYSTEM_GEMINI with GeminiVerdict schema) must keep the
# non-streaming _gemini_call — the API can't combine response_schema with
# streaming.
# ---------------------------------------------------------------------------


async def _claude_call_streaming(
    system: str,
    user: str,
    *,
    node: str,
    speaker: str = "claude",
    role: str = "explore",
    image_paths: list[str] | None = None,
) -> str:
    """Streaming version of _claude_call (no tools, text-only).

    For Claude-with-tools (Discovery's tooled mode) keep using
    _claude_tooled_call_streaming. v0.6.2: optional `image_paths` routes
    through the multimodal prompt builder.
    """
    collected: list[str] = []
    async for chunk in _claude_stream(system, user, image_paths=image_paths):
        collected.append(chunk)
        _emit_chunk(node, speaker, role, chunk)
    return "".join(collected)


# ---------------------------------------------------------------------------
# v0.6.1 — Symmetric Peer Review
#
# Design intent (user-locked): "they are working together correcting each
# other thinking together" — NOT "Gemini in command, Claude evaluates."
# So every verdict step runs BOTH brains in parallel:
#   1. Both emit their own structured verdict on the same evidence
#   2. Each one critiques the OTHER's verdict via VerdictEvaluation
#   3. Pipeline routes on the consensus, not on either side alone
#
# The VerdictEvaluation field ordering (identified_flaw_in_critique FIRST)
# breaks the LLM yes-man token trajectory before the decision token is
# sampled. Severity gating (critical/major/nit/none) prevents nit-level
# pushbacks from looping the pipeline.
# ---------------------------------------------------------------------------

from .state import VerdictEvaluation as _VerdictEvaluation  # noqa: E402


PEER_CRITIQUE_SYSTEM = """You are one half of the cothink dual-brain pair, evaluating your partner's verdict on the current pipeline stage.

You and your partner have equal standing. Neither of you is in command. Your job is to think TOGETHER — find any flaw in your partner's verdict, surface it, and decide whether it's load-bearing enough to warrant rework.

OUTPUT FORMAT: STRICT JSON matching this schema (no prose outside the JSON):
{
  "identified_flaw_in_critique": "<REQUIRED FIRST — at least one flaw in your partner's verdict. If you genuinely see none, say so explicitly with reasoning AFTER trying hard.>",
  "code_evidence": "<Quote the exact lines of code, contract bullet, or invariant text that grounds your flaw. Vague = rubber-stamping in disguise.>",
  "flaw_severity": "critical" | "major" | "nit" | "none",
  "final_decision": "CONCUR" | "PUSH_BACK",
  "pushback_reason": "<If PUSH_BACK: one-sentence summary of what your partner missed. Becomes replan context. Otherwise null.>"
}

RULES — the user's standing order is "fight, don't rubber-stamp" but ALSO "no false positives at 3 AM":
- Fill identified_flaw_in_critique FIRST. That field order is enforced — your token trajectory should start adversarial.
- ALWAYS attempt to identify a flaw. Surface nits even when you'd CONCUR — the user wants to see what you considered.
- Severity guide:
    critical = ship-stopper (security, data loss, prod outage class)
    major = real bug class the partner missed that would re-introduce in prod
    nit = pedantic / stylistic / inconsistency that's worth noting but does NOT block
    none = used only when you've genuinely tried hard and the partner's verdict is sound
- final_decision RULE: PUSH_BACK is ONLY valid when flaw_severity ∈ {critical, major}. Nits → CONCUR with the nit recorded.
- DO NOT push back on internal verdict-field inconsistencies that don't reflect a code defect.
- DO push back on: missed invariants when invariants exist in the contract; security CVE classes; race conditions; captured-LEARNINGS patterns the partner re-introduced.
- You are NOT in adversarial opposition to your partner — you are CORRECTING each other to converge on truth. Be precise, not pugnacious.
"""


async def _peer_critique(
    *,
    node: str,
    critic_speaker: str,           # "claude" | "gemini" — who is critiquing
    partner_speaker: str,          # "claude" | "gemini" — whose verdict is being critiqued
    partner_verdict_text: str,
    context_summary: str = "",
) -> _VerdictEvaluation:
    """One half of the symmetric peer review: critic_speaker evaluates partner_speaker's verdict.

    Streams chunks live so the user watches the critique form. Defensive
    CONCUR on parse failure so a single malformed response doesn't deadlock.
    """
    user_prompt = (
        f"YOUR PARTNER ({partner_speaker.upper()}) JUST EMITTED THIS VERDICT:\n"
        f"<partner_verdict speaker=\"{partner_speaker}\">\n"
        f"{partner_verdict_text[:8000]}\n"
        f"</partner_verdict>\n\n"
    )
    if context_summary:
        user_prompt += f"SHARED CONTEXT:\n{context_summary[:4000]}\n\n"
    user_prompt += (
        f"You are {critic_speaker.upper()}. Emit your VerdictEvaluation JSON now. "
        f"Find any flaw your partner missed, then decide whether it's load-bearing."
    )

    if critic_speaker == "claude":
        text = await _claude_call_streaming(
            PEER_CRITIQUE_SYSTEM,
            user_prompt,
            node=node,
            speaker="claude",
            role="critique",
        )
    else:
        text = await _gemini_call_streaming(
            PEER_CRITIQUE_SYSTEM,
            user_prompt,
            node=node,
            speaker="gemini",
            role="critique",
        )
    try:
        return _VerdictEvaluation.model_validate_json(_strip_codefence(text))
    except Exception:  # noqa: BLE001
        return _VerdictEvaluation(
            identified_flaw_in_critique=f"(parse failure — {critic_speaker}'s critique was not valid JSON)",
            code_evidence="(none — schema mishap)",
            flaw_severity="none",
            final_decision="CONCUR",
            pushback_reason=None,
        )


# Back-compat alias: callers using the old name still work.
_claude_devils_advocate = _peer_critique


async def _claude_verdict_call(
    system: str,
    user: str,
    schema_hint: str,
    *,
    node: str,
    role: str = "verdict",
) -> str:
    """Have Claude emit a structured verdict matching schema_hint.

    Streams the response live so the user watches Claude's verdict form
    alongside Gemini's. The schema description is appended to `system` so
    Claude knows the exact JSON shape to produce — no response_schema
    machinery on the Claude side (claude-agent-sdk doesn't expose one), so
    we lean on prompt + strict parsing instead.

    Returns the raw text for the caller to parse with whatever Pydantic
    model the schema_hint describes.
    """
    full_system = (
        f"{system}\n\n"
        f"OUTPUT FORMAT: STRICT JSON matching this schema (no prose outside the JSON):\n"
        f"{schema_hint}\n"
    )
    return await _claude_call_streaming(
        full_system, user, node=node, speaker="claude", role=role
    )


def _combine_peer_verdicts(
    claude_verdict: "GeminiVerdict",
    gemini_verdict: "GeminiVerdict",
    claude_critiques_gemini: _VerdictEvaluation,
    gemini_critiques_claude: _VerdictEvaluation,
) -> tuple["GeminiVerdict", list[str]]:
    """Combine two symmetric verdicts + two cross-critiques into one routing verdict.

    Logic:
      - If both raw verdicts are APPROVE AND neither critique is a critical/major PUSH_BACK:
          → consensus_APPROVE
      - If either raw verdict is VETO:
          → VETO (most conservative wins for halt-class decisions)
      - Otherwise:
          → REVISE, with revision_targets aggregating both sides' concerns

    Returns (combined_verdict, log_lines).
    """
    log: list[str] = []
    log.append(f"  claude_verdict={claude_verdict.decision}")
    log.append(f"  gemini_verdict={gemini_verdict.decision}")
    log.append(
        f"  claude_critiques_gemini={claude_critiques_gemini.final_decision}"
        f"({claude_critiques_gemini.flaw_severity})"
    )
    log.append(
        f"  gemini_critiques_claude={gemini_critiques_claude.final_decision}"
        f"({gemini_critiques_claude.flaw_severity})"
    )

    # Hard veto wins regardless of the other side.
    if claude_verdict.decision == "VETO" or gemini_verdict.decision == "VETO":
        veto_source = "claude" if claude_verdict.decision == "VETO" else "gemini"
        veto_rationale = (
            claude_verdict.rationale if veto_source == "claude" else gemini_verdict.rationale
        )
        log.append(f"  → VETO ({veto_source})")
        # Use the vetoer's verdict as the base, but record both rationales.
        base = claude_verdict if veto_source == "claude" else gemini_verdict
        return (
            base.model_copy(update={
                "rationale": (
                    f"[{veto_source.upper()} VETO] {veto_rationale}\n\n"
                    f"[partner verdict was {gemini_verdict.decision if veto_source == 'claude' else claude_verdict.decision}]"
                ),
            }),
            log,
        )

    both_approve = (
        claude_verdict.decision == "APPROVE" and gemini_verdict.decision == "APPROVE"
    )
    blocking_critique = (
        (claude_critiques_gemini.final_decision == "PUSH_BACK"
         and claude_critiques_gemini.flaw_severity in ("critical", "major"))
        or (gemini_critiques_claude.final_decision == "PUSH_BACK"
            and gemini_critiques_claude.flaw_severity in ("critical", "major"))
    )

    if both_approve and not blocking_critique:
        # Consensus APPROVE — merge invariants_evaluated from both (Gemini-side
        # tends to be more detailed but Claude may have flagged status changes).
        merged_invariants = list(claude_verdict.invariants_evaluated)
        seen_names = {s.name for s in merged_invariants}
        for s in gemini_verdict.invariants_evaluated:
            if s.name not in seen_names:
                merged_invariants.append(s)
        log.append("  → consensus APPROVE")
        return (
            gemini_verdict.model_copy(update={
                "decision": "APPROVE",
                "rationale": (
                    f"[CONSENSUS APPROVE]\n"
                    f"Claude: {claude_verdict.rationale}\n\n"
                    f"Gemini: {gemini_verdict.rationale}"
                ),
                "invariants_evaluated": merged_invariants,
            }),
            log,
        )

    # Otherwise REVISE — aggregate revision targets from both sides.
    targets = list(claude_verdict.revision_targets) + list(gemini_verdict.revision_targets)
    if (claude_critiques_gemini.final_decision == "PUSH_BACK"
            and claude_critiques_gemini.flaw_severity in ("critical", "major")):
        targets.append(
            f"[Claude flagged in Gemini's verdict] "
            f"{claude_critiques_gemini.pushback_reason or claude_critiques_gemini.identified_flaw_in_critique[:200]}"
        )
    if (gemini_critiques_claude.final_decision == "PUSH_BACK"
            and gemini_critiques_claude.flaw_severity in ("critical", "major")):
        targets.append(
            f"[Gemini flagged in Claude's verdict] "
            f"{gemini_critiques_claude.pushback_reason or gemini_critiques_claude.identified_flaw_in_critique[:200]}"
        )

    rationale_parts: list[str] = []
    if claude_verdict.decision != "APPROVE":
        rationale_parts.append(f"Claude said {claude_verdict.decision}: {claude_verdict.rationale}")
    if gemini_verdict.decision != "APPROVE":
        rationale_parts.append(f"Gemini said {gemini_verdict.decision}: {gemini_verdict.rationale}")
    if blocking_critique:
        rationale_parts.append(
            "Cross-critique found load-bearing flaws — see revision_targets."
        )

    log.append("  → REVISE (consensus or critique blocking)")
    return (
        gemini_verdict.model_copy(update={
            "decision": "REVISE",
            "rationale": "\n\n".join(rationale_parts) or "Disagreement — see revision_targets.",
            "revision_targets": targets,
            # Use Gemini's invariants_evaluated as the base; Claude's add on top.
            "invariants_evaluated": (
                gemini_verdict.invariants_evaluated + [
                    s for s in claude_verdict.invariants_evaluated
                    if s.name not in {gs.name for gs in gemini_verdict.invariants_evaluated}
                ]
            ),
        }),
        log,
    )


async def _gemini_call_streaming(
    system: str,
    user: str,
    *,
    node: str,
    speaker: str = "gemini",
    role: str = "explore",
    image_paths: list[str] | None = None,
) -> str:
    """Streaming version of _gemini_call. NOT compatible with response_schema.

    v0.6.2: optional `image_paths` routes through the multimodal Parts builder.
    """
    collected: list[str] = []
    async for chunk in _gemini_stream(system, user, image_paths=image_paths):
        collected.append(chunk)
        _emit_chunk(node, speaker, role, chunk)
    return "".join(collected)


async def _claude_tooled_call_streaming(
    system: str,
    user: str,
    *,
    node: str,
    speaker: str = "claude",
    role: str = "explore",
    allowed_tools: list[str],
    max_turns: int,
    cwd: str | None = None,
    image_paths: list[str] | None = None,
) -> str:
    """Streaming version of the tooled _claude_call used in Discovery.

    Streams TextBlock-by-TextBlock via the same claude-agent-sdk query loop
    as _claude_call but emits each text fragment as it arrives. Tool-result
    blocks are not emitted — only the narration text.

    v0.6.2: optional `image_paths` switches to the multimodal prompt so
    Claude sees pasted screenshots alongside the text request.
    """
    captured_stderr: list[str] = []
    opts_kwargs: dict = {
        "system_prompt": system,
        "allowed_tools": allowed_tools,
        "model": CLAUDE_MODEL,
        "max_turns": max_turns,
        "stderr": lambda line: captured_stderr.append(line),
        "permission_mode": "bypassPermissions",
        "setting_sources": [],
    }
    if cwd:
        opts_kwargs["cwd"] = cwd

    options = ClaudeAgentOptions(**opts_kwargs)
    chunks: list[str] = []
    prompt_arg = (
        _build_claude_multimodal_prompt(user, image_paths) if image_paths else user
    )
    try:
        async for message in claude_query(prompt=prompt_arg, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        chunks.append(block.text)
                        _emit_chunk(node, speaker, role, block.text)
                    elif isinstance(block, ToolUseBlock):
                        # Render tool call as a separate chunk role so the
                        # webview can style it like a terminal command line.
                        rendered = _format_tool_call(block.name, block.input)
                        _emit_chunk(node, speaker, "tool_call", rendered)
                    elif isinstance(block, ThinkingBlock):
                        # Surface Claude's internal reasoning if the SDK exposes it.
                        thinking_text = getattr(block, "thinking", None) or getattr(block, "text", "")
                        if thinking_text:
                            _emit_chunk(node, speaker, "thinking", str(thinking_text))
            elif isinstance(message, UserMessage):
                # The SDK echoes tool results inside UserMessage blocks. Surface
                # them so the user can see what Bash/Read returned.
                for block in message.content:
                    if isinstance(block, ToolResultBlock):
                        rendered = _format_tool_result(block.content, block.is_error)
                        role_name = "tool_error" if block.is_error else "tool_result"
                        _emit_chunk(node, speaker, role_name, rendered)
    except Exception as e:
        details = "\n".join(captured_stderr[-30:]) or "(no stderr captured)"
        raise RuntimeError(
            f"Claude tooled streaming call failed: {type(e).__name__}: {e}\n"
            f"tools={allowed_tools}, max_turns={max_turns}\n"
            f"--- last claude stderr ---\n{details}"
        ) from e
    return "".join(chunks)


def _format_tool_call(name: str, input_obj: Any) -> str:
    """Render a tool-call block as a single concise line for the UI.

    Bash → `$ <command>`. File ops show the path. Other tools show name+args.
    """
    if name == "Bash":
        cmd = ""
        if isinstance(input_obj, dict):
            cmd = str(input_obj.get("command", "")).strip()
        return f"$ {cmd}" if cmd else "$ (bash)"
    if name in ("Read", "Glob", "Grep") and isinstance(input_obj, dict):
        if name == "Read":
            return f"read {input_obj.get('file_path', '')}"
        if name == "Glob":
            return f"glob {input_obj.get('pattern', '')}"
        if name == "Grep":
            patt = input_obj.get("pattern", "")
            scope = input_obj.get("path") or input_obj.get("glob") or ""
            return f"grep {patt} {scope}".rstrip()
    return f"{name}({json.dumps(input_obj, default=str)[:120]})"


def _format_tool_result(content: Any, is_error: bool) -> str:
    """Render a tool-result block as plain text for the UI.

    Content can be a string or a list of blocks (the SDK normalizes this).
    `is_error` is part of the SDK contract; caller switches role based on it,
    but the formatter currently just returns the content verbatim either way.
    """
    del is_error
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text", item)))
            else:
                parts.append(str(item))
        text = "\n".join(parts)
    else:
        text = str(content)
    if len(text) > 4000:
        text = text[:4000] + f"\n…(truncated, {len(text)} chars total)"
    return text


# ---------------------------------------------------------------------------
# Dialogue tracking helper
# ---------------------------------------------------------------------------
def _add_dialogue(
    existing: list[DialogueEntry],
    node: str,
    speaker: str,
    role: str,
    content: str,
) -> list[DialogueEntry]:
    """Return existing + one new DialogueEntry. LangGraph state semantics
    require returning the FULL list (the node's return dict replaces the
    field, not appends to it)."""
    return existing + [
        DialogueEntry(node=node, speaker=speaker, role=role, content=content)
    ]


# ---------------------------------------------------------------------------
# Mechanism #4: git auto-checkpoint
# ---------------------------------------------------------------------------
# Both attribute form `<replan reason="..."/>` and block form `<replan>...</replan>`.
# Block form first because LLMs default to it more naturally per Gemini halu-check.
_REPLAN_BLOCK_RE = re.compile(r"<replan>\s*(.+?)\s*</replan>", re.DOTALL | re.IGNORECASE)
_REPLAN_ATTR_RE = re.compile(r'<replan\s+reason=[\'"](.+?)[\'"]\s*/?>', re.DOTALL)


def _git_checkpoint(project_dir: str, message: str) -> tuple[bool, str, str | None]:
    """Stage all + commit if project_dir is a git repo. Returns (ok, info, commit_hash).

    Silent no-op when the directory isn't a git repo. Uses --allow-empty so
    back-to-back checkpoints don't fail when nothing changed. Uses --no-verify
    to bypass user-configured pre-commit hooks (linters/tests would otherwise
    block checkpoints made while code is in a transitional broken state).
    """
    if not (Path(project_dir) / ".git").exists():
        return False, "not a git repo", None
    try:
        subprocess.run(
            ["git", "add", "-A"],
            cwd=project_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "commit", "--allow-empty", "--no-verify", "-m", message],
            cwd=project_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        rev = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        return True, "committed", rev.stdout.strip()
    except subprocess.CalledProcessError as e:
        return False, f"git failed: {(e.stderr or e.stdout or '')[:120]}", None


def _git_is_dirty(project_dir: str) -> tuple[bool, str]:
    """Returns (is_dirty, status_output) for a git repo, or (False, '') if no repo."""
    if not (Path(project_dir) / ".git").exists():
        return False, ""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        return bool(result.stdout.strip()), result.stdout
    except subprocess.CalledProcessError:
        return False, ""


def _detect_replan(claude_text: str) -> str | None:
    """Return the replan reason if Claude emitted any <replan> form."""
    m = _REPLAN_BLOCK_RE.search(claude_text)
    if m:
        return m.group(1).strip()
    m = _REPLAN_ATTR_RE.search(claude_text)
    if m:
        return m.group(1).strip()
    return None


def _list_project_files(project_dir: str, max_files: int = 200) -> list[str]:
    base = Path(project_dir)
    if not base.exists():
        return []
    skip = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}
    out: list[str] = []
    for p in base.rglob("*"):
        if any(part in skip for part in p.parts):
            continue
        if p.is_file():
            out.append(str(p.relative_to(base)))
            if len(out) >= max_files:
                break
    return out


# v0.6.6 Context Chips — pre-read @-mentioned files into the Discovery prompt
# so Claude/Gemini start grounded instead of re-discovering via tools.
#
# Each mention dict: {"kind": "file"|"folder"|"symbol", "path": "...", "symbol": ""}
# Read budget caps at MAX_MENTION_BYTES total; per-file slice capped at
# MAX_PER_FILE_BYTES. Folders are listed (not read) — too easy to blow the
# context budget on a whole dir of code.
_MAX_MENTION_BYTES = 32_000
_MAX_PER_FILE_BYTES = 8_000


def _build_mentions_block(mentions: list[dict], project_dir: str) -> str:
    if not mentions:
        return ""
    base = Path(project_dir).resolve()
    parts: list[str] = [
        "USER-ATTACHED CONTEXT (the user explicitly @-mentioned these — treat as primary, not optional):\n"
    ]
    budget_left = _MAX_MENTION_BYTES
    for m in mentions:
        kind = (m.get("kind") or "").lower()
        rel = (m.get("path") or "").strip()
        if not rel:
            continue
        # Resolve and reject anything outside project_dir.
        candidate = (base / rel).resolve()
        try:
            candidate.relative_to(base)
        except ValueError:
            parts.append(f"  ⚠ skipped {rel!r} — outside project_dir\n")
            continue

        if kind == "folder":
            if candidate.is_dir():
                kids = []
                for child in sorted(candidate.iterdir())[:30]:
                    suffix = "/" if child.is_dir() else ""
                    kids.append(f"    - {child.name}{suffix}")
                parts.append(
                    f"\n@folder {rel} (listing only — read individual files via Read tool if needed):\n"
                    + "\n".join(kids)
                    + "\n"
                )
            else:
                parts.append(f"  ⚠ @folder {rel}: not a directory\n")
            continue

        if kind in ("file", "symbol"):
            if not candidate.is_file():
                parts.append(f"  ⚠ @{kind} {rel}: file not found\n")
                continue
            try:
                content = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                parts.append(f"  ⚠ @{kind} {rel}: read failed ({type(e).__name__})\n")
                continue
            slice_budget = min(_MAX_PER_FILE_BYTES, budget_left)
            if slice_budget <= 0:
                parts.append(f"  ⚠ @{kind} {rel}: skipped (context budget exhausted)\n")
                continue
            slice_text = content[:slice_budget]
            truncated = len(content) > slice_budget
            symbol_hint = ""
            if kind == "symbol" and m.get("symbol"):
                symbol_hint = f" (user pinned symbol: {m['symbol']!r})"
            parts.append(
                f"\n@{kind} {rel}{symbol_hint}"
                f" ({len(content)} bytes, showing first {len(slice_text)}):\n"
                f"```\n{slice_text}\n"
                f"```\n"
            )
            if truncated:
                parts.append(f"  …(truncated — full file at {rel})\n")
            budget_left -= len(slice_text)
            continue

        # Unknown kind — surface so the user can debug their composer chip.
        parts.append(f"  ⚠ unknown mention kind {kind!r} for {rel}\n")

    parts.append("\nEND USER-ATTACHED CONTEXT.\n\n")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Node 1: Discovery
# ---------------------------------------------------------------------------
async def discovery_node(state: CothinkState) -> dict:
    files = _list_project_files(state.project_dir)

    # Cross-run memory: load prior learnings if present (mechanism for
    # "cothink remembers what we decided last time in this project").
    learnings_md, learnings_count = load_learnings(state.project_dir)
    learnings_block = ""
    if learnings_md.strip():
        learnings_block = (
            f"## PRIOR LEARNINGS FROM PREVIOUS COTHINK RUNS\n"
            f"(Loaded from _collab/LEARNINGS.md — these are decisions and rules "
            f"we've already committed to in this project. Honor them unless this "
            f"task explicitly contradicts.)\n\n"
            f"{learnings_md}\n"
            f"## END PRIOR LEARNINGS\n\n"
        )

    # v0.6.5: prepend the project state journal so both brains start the
    # turn grounded in state-of-the-world facts (what's pending, what's
    # drafted-not-sent, etc.). Empty string when no journal exists yet.
    # This is the cross-turn memory primitive that survives compaction
    # because it's a file, not a transcript line.
    project_state_block = project_state_journal.load_journal_for_prompt(
        state.project_dir, max_chars=4000
    )

    # v0.6.2: surface attached image references in the text so both brains
    # know the screenshots are PART of the user request (not separate context).
    image_note = ""
    if state.attached_images:
        image_note = (
            f"\nATTACHED IMAGES ({len(state.attached_images)} screenshots from "
            f"the user, downsampled ≤1024px and provided as inline multimodal "
            f"input — describe what you see and treat them as primary context "
            f"for the request).\n\n"
        )

    # v0.6.6 Context Chips: the user explicitly @-mentioned files/folders/
    # symbols in the composer. Pre-read them now and fold into user_msg so
    # Discovery starts grounded rather than wasting tool calls re-discovering
    # what the user already pointed at.
    mentions_block = _build_mentions_block(state.attached_mentions, state.project_dir)

    user_msg = (
        f"{project_state_block}"
        f"{learnings_block}"
        f"USER REQUEST:\n{state.user_request}\n"
        f"{image_note}\n"
        f"{mentions_block}"
        f"PROJECT FILES (truncated to 200; you may Glob/Read/Grep for more):\n"
        f"{json.dumps(files, indent=2)}\n\n"
        f"Build a shared mental model. Output JSON with keys: "
        f"existing_files, ambiguities, assumptions_to_verify, key_questions."
    )
    # v0.5-step-1: Claude gets real workspace tools for Discovery; Gemini stays
    # text-only (passive analysis of the file listing + user_request). Claude
    # explores the codebase via Read/Glob/Grep before composing his discovery
    # summary. cwd scopes the tools to the project; max_turns=10 gives ~3-8
    # tool calls of headroom before the final text reply.
    # v0.5.4-streaming: route through the streaming helpers so the webview sees
    # live tokens through the chunk_emitter contextvar.
    # v0.5.4-bash: Claude also gets Bash so it can `wc -l`, `head`, `git status`,
    # `tree`, `cat` files at absolute paths the user mentions, etc. — same
    # terminal-level visibility Claude Code provides. cwd=project_dir still
    # scopes default-relative paths but absolute paths are honored.
    # v0.6.2: pass attached_images so Claude (multimodal) sees the screenshots
    # inline; same for Gemini via the streaming helper below.
    claude_view = await _claude_tooled_call_streaming(
        DISCOVERY_SYSTEM_CLAUDE_TOOLED,
        user_msg,
        node="discovery",
        speaker="claude",
        role="explore",
        allowed_tools=["Read", "Glob", "Grep", "Bash"],
        max_turns=15,
        cwd=state.project_dir,
        image_paths=state.attached_images or None,
    )
    gemini_view = await _gemini_call_streaming(
        DISCOVERY_SYSTEM_BOTH,
        user_msg,
        node="discovery",
        speaker="gemini",
        role="explore",
        image_paths=state.attached_images or None,
    )

    dialogue = _add_dialogue(state.dialogue, "discovery", "claude", "explore", claude_view)
    dialogue = _add_dialogue(dialogue, "discovery", "gemini", "explore", gemini_view)

    understanding = {
        "claude_view": claude_view,
        "gemini_view": gemini_view,
        "project_files_sample": files[:50],
    }
    log_lines = [f"[discovery] complete (loaded {learnings_count} prior learnings)"]
    return {
        "understanding": understanding,
        "discovery_complete": True,
        "learnings_loaded": learnings_count,
        "dialogue": dialogue,
        "log": state.log + log_lines,
    }


# ---------------------------------------------------------------------------
# Node 2: Planning
# ---------------------------------------------------------------------------
async def planning_node(state: CothinkState) -> dict:
    context = (
        f"USER REQUEST:\n{state.user_request}\n\n"
        f"SHARED UNDERSTANDING:\n{json.dumps(state.understanding, indent=2)[:8000]}\n\n"
    )
    if state.replan_reason:
        context += (
            f"REPLAN TRIGGERED — the previous execution attempt was abandoned because:\n"
            f"  {state.replan_reason}\n"
            f"Factor this into the new plan. The orchestrator has already committed a "
            f"git checkpoint preserving the prior state.\n\n"
        )
    context += "Produce the bounded design contract (≤50 bullets, [INVARIANT] tags) and the invariants list."
    claude_proposal = await _claude_call_streaming(
        PLANNING_SYSTEM_BOTH, context, node="planning", speaker="claude", role="propose"
    )
    gemini_proposal = await _gemini_call_streaming(
        PLANNING_SYSTEM_BOTH, context, node="planning", speaker="gemini", role="propose"
    )
    dialogue = _add_dialogue(state.dialogue, "planning", "claude", "propose", claude_proposal)
    dialogue = _add_dialogue(dialogue, "planning", "gemini", "propose", gemini_proposal)

    merge_prompt = (
        f"CLAUDE'S DRAFT:\n<claude_proposal>\n{claude_proposal}\n</claude_proposal>\n\n"
        f"GEMINI'S DRAFT:\n<gemini_proposal>\n{gemini_proposal}\n</gemini_proposal>\n\n"
        f"Produce ONE merged JSON object: {{\"contract\": [...], \"invariants\": [{{name, description}}, ...]}}"
    )
    merged_text = await _gemini_call_streaming(
        PLANNING_SYSTEM_BOTH, merge_prompt, node="planning", speaker="gemini", role="merge"
    )
    dialogue = _add_dialogue(dialogue, "planning", "gemini", "merge", merged_text)
    try:
        merged = json.loads(_strip_codefence(merged_text))
    except json.JSONDecodeError:
        return {
            "counters": state.counters.model_copy(update={"replans": state.counters.replans + 1}),
            "dialogue": dialogue,
            "log": state.log + ["[planning] failed to parse merged contract"],
        }

    contract = merged.get("contract", [])
    invariants = [Invariant(**i) for i in merged.get("invariants", [])]

    # v0.6.4 free win: anti-position-bias structure. The [INVARIANT] block
    # lands AFTER the main context but BEFORE the final emit instruction —
    # parking it in the recency-sensitive zone so attention dilution can't
    # bury it in the middle of a long context. Gemini debate caveat: do NOT
    # make invariants the literal last text; the emit instruction must be
    # last so the model doesn't forget output format.
    invariants_block = (
        f"=== CRITICAL INVARIANTS (re-read before deciding) ===\n"
        f"{json.dumps([i.model_dump() for i in invariants], indent=2)}\n"
        f"=== END INVARIANTS ===\n\n"
    )
    verdict_prompt = (
        f"CANDIDATE CONTRACT:\n{json.dumps(contract, indent=2)}\n\n"
        f"{invariants_block}"
        f"Emit your verdict. invariants_evaluated MUST cover EVERY invariant above."
    )

    # v0.6.1 SYMMETRIC: both brains verdict in parallel, both critique in parallel.
    import asyncio as _asyncio
    claude_verdict_text, gemini_verdict_text = await _asyncio.gather(
        _claude_verdict_call(
            PLANNING_VERDICT_GEMINI, verdict_prompt, _GEMINI_VERDICT_SCHEMA_HINT,
            node="planning", role="verdict",
        ),
        _gemini_call(PLANNING_VERDICT_GEMINI, verdict_prompt, GeminiVerdict),
    )
    dialogue = _add_dialogue(dialogue, "planning", "claude", "verdict", claude_verdict_text)
    dialogue = _add_dialogue(dialogue, "planning", "gemini", "verdict", gemini_verdict_text)

    try:
        claude_verdict = GeminiVerdict.model_validate_json(_strip_codefence(claude_verdict_text))
    except Exception as e:  # noqa: BLE001
        return {
            "counters": state.counters.model_copy(update={"replans": state.counters.replans + 1}),
            "dialogue": dialogue,
            "log": state.log + [f"[planning] claude verdict parse error: {e}"],
        }
    try:
        gemini_verdict = GeminiVerdict.model_validate_json(_strip_codefence(gemini_verdict_text))
    except Exception as e:  # noqa: BLE001
        return {
            "counters": state.counters.model_copy(update={"replans": state.counters.replans + 1}),
            "dialogue": dialogue,
            "log": state.log + [f"[planning] gemini verdict parse error: {e}"],
        }

    # Each brain critiques the OTHER, in parallel.
    crit_context = (
        f"CONTRACT BULLETS (n={len(contract)}): {json.dumps(contract[:10])}\n"
        f"INVARIANTS (n={len(invariants)}): {json.dumps([i.model_dump() for i in invariants[:5]])}"
    )
    claude_critiques_gemini, gemini_critiques_claude = await _asyncio.gather(
        _peer_critique(
            node="planning", critic_speaker="claude", partner_speaker="gemini",
            partner_verdict_text=gemini_verdict_text, context_summary=crit_context,
        ),
        _peer_critique(
            node="planning", critic_speaker="gemini", partner_speaker="claude",
            partner_verdict_text=claude_verdict_text, context_summary=crit_context,
        ),
    )
    dialogue = _add_dialogue(
        dialogue, "planning", "claude", "critique", claude_critiques_gemini.model_dump_json()
    )
    dialogue = _add_dialogue(
        dialogue, "planning", "gemini", "critique", gemini_critiques_claude.model_dump_json()
    )

    verdict, combine_log = _combine_peer_verdicts(
        claude_verdict, gemini_verdict, claude_critiques_gemini, gemini_critiques_claude
    )
    plan_approved_final = verdict.decision == "APPROVE"
    log_lines = ["[planning] symmetric peer review:"] + combine_log

    return {
        "design_contract": contract,
        "invariants": invariants,
        "plan_approved": plan_approved_final,
        "last_verdict": verdict,
        "replan_reason": None,  # consumed; clear so executing doesn't re-route to planning
        "dialogue": dialogue,
        "verdict_evaluations": state.verdict_evaluations + [
            claude_critiques_gemini,
            gemini_critiques_claude,
        ],
        "log": state.log + log_lines,
    }


# ---------------------------------------------------------------------------
# Node 3: Executing
# ---------------------------------------------------------------------------
async def executing_node(state: CothinkState) -> dict:
    # Mechanism #4: pre-execute git checkpoint (once per run, before first write).
    # Captures the user's pre-cothink state so any cothink writes are revertible.
    pre_execute_log: list[str] = []
    pre_execute_hash: str | None = state.pre_execute_commit_hash
    if not state.pre_execute_checkpointed:
        ok, info, sha = _git_checkpoint(state.project_dir, "cothink: pre-execute checkpoint")
        pre_execute_hash = sha
        pre_execute_log.append(f"[executing] pre-execute checkpoint: {info}{f' ({sha[:8]})' if sha else ''}")

    context = (
        f"DESIGN CONTRACT:\n{json.dumps(state.design_contract, indent=2)}\n\n"
        f"INVARIANTS:\n{json.dumps([i.model_dump() for i in state.invariants], indent=2)}\n\n"
        f"USER REQUEST:\n{state.user_request}\n\n"
    )
    if state.last_stderr:
        context += f"PREVIOUS MECHANICAL GATE FAILURE:\n{state.last_stderr}\n\nFix the failure.\n\n"
    if state.last_verdict and state.last_verdict.decision == "REVISE":
        context += (
            f"PREVIOUS REVISION REQUEST:\n"
            f"rationale: {state.last_verdict.rationale}\n"
            f"targets: {state.last_verdict.revision_targets}\n\n"
        )

    context += (
        "Output JSON: {\"diffs\": [{\"file_path\": \"...\", \"content\": \"...\", "
        "\"contract_bullet_quoted\": \"...\", \"rationale\": \"...\"}, ...]}\n"
        "Each diff MUST include a verbatim quote of the contract bullet it fulfills.\n"
        "If you cannot satisfy the contract without violating an invariant, instead emit "
        "a single line: <replan reason=\"...\"/> — the orchestrator will checkpoint and "
        "drop back to Planning."
    )

    claude_text = await _claude_call_streaming(
        EXECUTING_SYSTEM_CLAUDE, context, node="executing", speaker="claude", role="propose"
    )
    dialogue = _add_dialogue(state.dialogue, "executing", "claude", "propose", claude_text)

    # Mechanism #4: detect replan request before attempting to parse diffs.
    replan_reason = _detect_replan(claude_text)
    if replan_reason:
        # Mechanism #5: short-circuit if Claude is repeating itself.
        if replan_reason in state.replan_history:
            return {
                "pre_execute_checkpointed": True,
                "pre_execute_commit_hash": pre_execute_hash,
                "dialogue": dialogue,
                "halt_reason": f"repeated_replan: {replan_reason[:80]}",
                "log": state.log + pre_execute_log + [
                    f"[executing] REPEATED REPLAN reason ('{replan_reason[:60]}'); "
                    f"escalating to Human Fallback to avoid token-burn loop"
                ],
            }
        ok, info, sha = _git_checkpoint(
            state.project_dir, f"cothink: pre-replan ({replan_reason[:60]})"
        )
        return {
            "replan_reason": replan_reason,
            "replan_history": state.replan_history + [replan_reason],
            "plan_approved": False,
            "pre_execute_checkpointed": True,
            "pre_execute_commit_hash": pre_execute_hash,
            "dialogue": dialogue,
            "counters": state.counters.model_copy(
                update={"replans": state.counters.replans + 1}
            ),
            "log": state.log + pre_execute_log + [
                f"[executing] REPLAN triggered: {replan_reason[:80]} (checkpoint: {info})"
            ],
        }

    try:
        parsed = json.loads(_strip_codefence(claude_text))
        diffs = [ProposedDiff(**d) for d in parsed.get("diffs", [])]
    except Exception as e:  # noqa: BLE001
        return {
            "counters": state.counters.model_copy(update={"revisions": state.counters.revisions + 1}),
            "pre_execute_checkpointed": True,
            "pre_execute_commit_hash": pre_execute_hash,
            "dialogue": dialogue,
            "log": state.log + pre_execute_log + [f"[executing] claude diff parse error: {e}"],
        }

    if not diffs:
        return {
            "pre_execute_checkpointed": True,
            "pre_execute_commit_hash": pre_execute_hash,
            "dialogue": dialogue,
            "log": state.log + pre_execute_log + ["[executing] no diffs proposed"],
            "halt_reason": "executing_no_diffs",
        }

    # v0.6.4 free win: invariants in the recency-anchored zone (post-context,
    # pre-emit) so they're not buried in the middle when proposed_diffs are big.
    invariants_block = (
        f"=== CRITICAL INVARIANTS (re-read before deciding) ===\n"
        f"{json.dumps([i.model_dump() for i in state.invariants], indent=2)}\n"
        f"=== END INVARIANTS ===\n\n"
    )
    verdict_prompt = (
        f"PROPOSED DIFFS:\n<diffs>\n{json.dumps([d.model_dump() for d in diffs], indent=2)}\n</diffs>\n\n"
        f"{invariants_block}"
        f"Steelman the proposal first, then evaluate. invariants_evaluated MUST cover every invariant above."
    )

    # v0.6.1 SYMMETRIC peer review: both brains verdict in parallel,
    # both critique in parallel. Neither is in command.
    import asyncio as _asyncio
    claude_verdict_text, gemini_verdict_text = await _asyncio.gather(
        _claude_verdict_call(
            EXECUTING_VERDICT_GEMINI, verdict_prompt, _GEMINI_VERDICT_SCHEMA_HINT,
            node="executing", role="verdict",
        ),
        _gemini_call(EXECUTING_VERDICT_GEMINI, verdict_prompt, GeminiVerdict),
    )
    dialogue = _add_dialogue(dialogue, "executing", "claude", "verdict", claude_verdict_text)
    dialogue = _add_dialogue(dialogue, "executing", "gemini", "verdict", gemini_verdict_text)

    try:
        claude_verdict = GeminiVerdict.model_validate_json(_strip_codefence(claude_verdict_text))
    except Exception as e:  # noqa: BLE001
        return {
            "counters": state.counters.model_copy(update={"revisions": state.counters.revisions + 1}),
            "pre_execute_checkpointed": True,
            "pre_execute_commit_hash": pre_execute_hash,
            "dialogue": dialogue,
            "log": state.log + pre_execute_log + [f"[executing] claude verdict parse error: {e}"],
        }
    try:
        gemini_verdict = GeminiVerdict.model_validate_json(_strip_codefence(gemini_verdict_text))
    except Exception as e:  # noqa: BLE001
        return {
            "counters": state.counters.model_copy(update={"revisions": state.counters.revisions + 1}),
            "pre_execute_checkpointed": True,
            "pre_execute_commit_hash": pre_execute_hash,
            "dialogue": dialogue,
            "log": state.log + pre_execute_log + [f"[executing] gemini verdict parse error: {e}"],
        }

    crit_context = (
        f"PROPOSED DIFFS (n={len(diffs)}, paths): "
        f"{json.dumps([d.file_path for d in diffs[:5]])}\n"
        f"INVARIANTS: {json.dumps([i.model_dump() for i in state.invariants[:5]])}"
    )
    claude_critiques_gemini, gemini_critiques_claude = await _asyncio.gather(
        _peer_critique(
            node="executing", critic_speaker="claude", partner_speaker="gemini",
            partner_verdict_text=gemini_verdict_text, context_summary=crit_context,
        ),
        _peer_critique(
            node="executing", critic_speaker="gemini", partner_speaker="claude",
            partner_verdict_text=claude_verdict_text, context_summary=crit_context,
        ),
    )
    dialogue = _add_dialogue(
        dialogue, "executing", "claude", "critique", claude_critiques_gemini.model_dump_json()
    )
    dialogue = _add_dialogue(
        dialogue, "executing", "gemini", "critique", gemini_critiques_claude.model_dump_json()
    )

    verdict, combine_log = _combine_peer_verdicts(
        claude_verdict, gemini_verdict, claude_critiques_gemini, gemini_critiques_claude
    )
    pre_execute_log.append("[executing] symmetric peer review:")
    pre_execute_log.extend(combine_log)

    eval_list_update = state.verdict_evaluations + [
        claude_critiques_gemini,
        gemini_critiques_claude,
    ]

    if verdict.decision == "VETO":
        return {
            "last_verdict": verdict,
            "pre_execute_checkpointed": True,
            "pre_execute_commit_hash": pre_execute_hash,
            "dialogue": dialogue,
            "verdict_evaluations": eval_list_update,
            "halt_reason": f"executing_veto: {verdict.rationale}",
            "log": state.log + pre_execute_log + [f"[executing] VETO: {verdict.rationale}"],
        }

    if verdict.decision == "REVISE":
        return {
            "last_verdict": verdict,
            "pre_execute_checkpointed": True,
            "pre_execute_commit_hash": pre_execute_hash,
            "dialogue": dialogue,
            "verdict_evaluations": eval_list_update,
            "counters": state.counters.model_copy(
                update={"revisions": state.counters.revisions + 1}
            ),
            "log": state.log + pre_execute_log + [f"[executing] REVISE x{state.counters.revisions + 1}"],
        }

    written: list[ProposedDiff] = []
    for d in diffs:
        target = (Path(state.project_dir) / d.file_path).resolve()
        if not str(target).startswith(str(Path(state.project_dir).resolve())):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(d.content, encoding="utf-8")
        written.append(d)

    return {
        "proposed_diffs": written,
        "last_verdict": verdict,
        "untested_diffs": True,
        "last_stderr": None,
        "pre_execute_checkpointed": True,
        "pre_execute_commit_hash": pre_execute_hash,
        "dialogue": dialogue,
        "verdict_evaluations": eval_list_update,
        "log": state.log + pre_execute_log + [f"[executing] APPROVE; wrote {len(written)} files"],
    }


# ---------------------------------------------------------------------------
# Node 4: Mechanical Gate (NO LLM, deterministic)
# ---------------------------------------------------------------------------
def mechanical_node(state: CothinkState) -> dict:
    errors: list[str] = []
    for d in state.proposed_diffs:
        if not d.file_path.endswith(".py"):
            continue
        target = (Path(state.project_dir) / d.file_path).resolve()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                py_compile.compile(
                    str(target),
                    cfile=str(Path(tmp) / "out.pyc"),
                    doraise=True,
                )
        except py_compile.PyCompileError as e:
            errors.append(str(e))

    if errors:
        return {
            "mechanical_pass": False,
            "untested_diffs": True,
            "last_stderr": "\n---\n".join(errors),
            "counters": state.counters.model_copy(
                update={"mechanical_fails": state.counters.mechanical_fails + 1}
            ),
            "log": state.log + [f"[mechanical] FAIL ({len(errors)} errors)"],
        }

    return {
        "mechanical_pass": True,
        "untested_diffs": False,
        "last_stderr": None,
        "log": state.log + ["[mechanical] PASS"],
    }


# ---------------------------------------------------------------------------
# Node 5: Contract Review — SYMMETRIC PEER REVIEW (v0.6.1)
#
# Both Claude and Gemini emit their own verdict in parallel, then each
# critiques the OTHER's verdict. The orchestrator combines via
# _combine_peer_verdicts. Neither brain is in command — they think together.
# ---------------------------------------------------------------------------

# Schema description fed to Claude so it emits the same GeminiVerdict shape
# that Gemini does (claude-agent-sdk has no native response_schema; prompt
# + strict parsing is the substitute).
_GEMINI_VERDICT_SCHEMA_HINT = """{
  "decision": "APPROVE" | "REVISE" | "VETO",
  "rationale": "<paragraph explaining your verdict>",
  "invariants_evaluated": [
    {"name": "<invariant name>", "status": "pass" | "fail" | "unknown", "reason": "<one-line reason>"}
  ],
  "revision_targets": ["<bullet 1>", "<bullet 2>"],
  "build_needed": true | false
}"""


async def contract_review_node(state: CothinkState) -> dict:
    delivered: dict[str, str] = {}
    for d in state.proposed_diffs:
        target = Path(state.project_dir) / d.file_path
        if target.exists():
            delivered[d.file_path] = target.read_text(encoding="utf-8")[:8000]

    # v0.6.4 free win: BIGGEST anti-position-bias win lives here — delivered
    # files can be ~12KB; without restructuring, the invariants land in the
    # MIDDLE of the prompt where Lost-in-the-Middle has worst recall. Move
    # invariants AFTER the files block, just before the final emit instruction.
    invariants_block = (
        f"=== CRITICAL INVARIANTS (re-read before deciding) ===\n"
        f"{json.dumps([i.model_dump() for i in state.invariants], indent=2)}\n"
        f"=== END INVARIANTS ===\n\n"
    )
    review_prompt = (
        f"DESIGN CONTRACT:\n{json.dumps(state.design_contract, indent=2)}\n\n"
        f"DELIVERED FILES:\n{json.dumps(delivered, indent=2)[:12000]}\n\n"
        f"{invariants_block}"
        f"Verify each contract bullet, especially [INVARIANT] bullets. "
        f"invariants_evaluated MUST cover EVERY invariant in the contract above."
    )

    # v0.6.1 SYMMETRIC: Both brains emit their own verdict in parallel.
    # Neither is "in command" — they think together on the same evidence.
    import asyncio as _asyncio

    claude_verdict_text, gemini_verdict_text = await _asyncio.gather(
        _claude_verdict_call(
            CONTRACT_REVIEW_SYSTEM_GEMINI,  # same review framing for both
            review_prompt,
            _GEMINI_VERDICT_SCHEMA_HINT,
            node="contract_review",
            role="review",
        ),
        _gemini_call(CONTRACT_REVIEW_SYSTEM_GEMINI, review_prompt, GeminiVerdict, use_pro=True),
    )
    dialogue = _add_dialogue(
        state.dialogue, "contract_review", "claude", "review", claude_verdict_text
    )
    dialogue = _add_dialogue(
        dialogue, "contract_review", "gemini", "review", gemini_verdict_text
    )

    # Parse both verdicts. Defensive on each.
    try:
        claude_verdict = GeminiVerdict.model_validate_json(_strip_codefence(claude_verdict_text))
    except Exception as e:  # noqa: BLE001
        return {
            "counters": state.counters.model_copy(
                update={"contract_fails": state.counters.contract_fails + 1}
            ),
            "dialogue": dialogue,
            "log": state.log + [f"[contract_review] claude verdict parse error: {e}"],
        }
    try:
        gemini_verdict = GeminiVerdict.model_validate_json(_strip_codefence(gemini_verdict_text))
    except Exception as e:  # noqa: BLE001
        return {
            "counters": state.counters.model_copy(
                update={"contract_fails": state.counters.contract_fails + 1}
            ),
            "dialogue": dialogue,
            "log": state.log + [f"[contract_review] gemini verdict parse error: {e}"],
        }

    # Each brain critiques the OTHER's verdict, in parallel.
    crit_context = (
        f"INVARIANTS DECLARED: "
        f"{json.dumps([i.model_dump() for i in state.invariants[:8]])}"
    )
    claude_critiques_gemini, gemini_critiques_claude = await _asyncio.gather(
        _peer_critique(
            node="contract_review",
            critic_speaker="claude",
            partner_speaker="gemini",
            partner_verdict_text=gemini_verdict_text,
            context_summary=crit_context,
        ),
        _peer_critique(
            node="contract_review",
            critic_speaker="gemini",
            partner_speaker="claude",
            partner_verdict_text=claude_verdict_text,
            context_summary=crit_context,
        ),
    )
    dialogue = _add_dialogue(
        dialogue, "contract_review", "claude", "critique", claude_critiques_gemini.model_dump_json()
    )
    dialogue = _add_dialogue(
        dialogue, "contract_review", "gemini", "critique", gemini_critiques_claude.model_dump_json()
    )

    # Combine into one routing verdict.
    verdict, combine_log = _combine_peer_verdicts(
        claude_verdict, gemini_verdict, claude_critiques_gemini, gemini_critiques_claude
    )
    statuses = verdict.invariants_evaluated
    all_pass = all(s.status == "pass" for s in statuses) and verdict.decision == "APPROVE"

    extra_log = [f"[contract_review] symmetric peer review:"]
    extra_log.extend(combine_log)

    if all_pass:
        # Cross-run memory: persist a learning so the next cothink run in this
        # project loads the decision/rule as Discovery context. Mechanical entry
        # built from state — v0.4 polish would have an LLM write higher-quality entries.
        try:
            entry = LearningEntry(
                component=state.user_request[:80],
                decision=(state.design_contract[0] if state.design_contract else "(no contract bullets)"),
                rule="; ".join(i.name for i in state.invariants) or "(no invariants)",
                run_id=str(uuid.uuid4())[:8],
                timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
            append_learning(state.project_dir, entry)
            extra_log.append("[contract_review] appended LearningEntry to _collab/LEARNINGS.md")
        except Exception as e:  # noqa: BLE001
            # Memory persistence is best-effort; never block the run on a write failure.
            extra_log.append(f"[contract_review] LearningEntry append failed: {e}")

    return {
        "contract_status": statuses,
        "last_verdict": verdict,
        "counters": state.counters if all_pass else state.counters.model_copy(
            update={"contract_fails": state.counters.contract_fails + 1}
        ),
        "dialogue": dialogue,
        "verdict_evaluations": state.verdict_evaluations + [
            claude_critiques_gemini,
            gemini_critiques_claude,
        ],
        "log": state.log + [f"[contract_review] decision={verdict.decision}"] + extra_log,
    }


# ---------------------------------------------------------------------------
# v0.6.5 — Project State Journal updater (post-APPROVE only)
#
# Runs after Contract Review APPROVES. One Gemini Pro call emits the full
# current state-of-the-world (pending / drafted_not_sent / confirmed_facts /
# open_questions + turn_summary). The harness renders to
# _collab/project_state.md. Discovery prepends the journal at the next
# turn so both brains start grounded — bypasses attention dilution by
# decoupling FACTS from the noisy transcript.
#
# Locked design (Gemini debate, confidence 0.95):
#   - NEVER runs on REVISE / VETO turns (Planning is intent, not reality;
#     only APPROVED outcomes belong in the journal).
#   - Best-effort: journal-update failures NEVER fail the turn. The turn
#     has already passed its integrity gates; the journal is a memory
#     mechanism, not a gate.
# ---------------------------------------------------------------------------
async def project_state_node(state: CothinkState) -> dict:
    """Update _collab/project_state.md with the current state-of-the-world.

    Always invoked after contract_review_node. It self-skips for non-APPROVE
    verdicts (REVISE/VETO routed back to executing; we don't reach here
    until the consensus is APPROVE — but check defensively).
    """
    if state.last_verdict is None or state.last_verdict.decision != "APPROVE":
        return {
            "log": state.log + ["[project_state] skipped (not APPROVE)"],
        }

    prior_journal = project_state_journal.load_journal(state.project_dir)
    prior_block = (
        f"PRIOR JOURNAL (carry forward what's still valid, resolve what's "
        f"resolved, add what's new):\n<prior>\n{prior_journal[:8000]}\n</prior>\n\n"
        if prior_journal.strip()
        else "PRIOR JOURNAL: (none — this is the first entry)\n\n"
    )

    contract_summary = (
        f"CONTRACT (last delivered):\n{json.dumps(state.design_contract[:15], indent=2)}\n\n"
        if state.design_contract
        else ""
    )

    # Recent dialogue is the LLM's only window into "what just happened this
    # turn." Sample the tail so we stay bounded; the journal updater needs
    # facts (what was said, what was decided), not full critiques.
    recent_dialogue = state.dialogue[-12:] if state.dialogue else []
    dialogue_block = (
        f"THIS TURN'S DIALOGUE TAIL (most recent {len(recent_dialogue)} entries):\n"
        + "\n".join(
            f"[{e.node}/{e.speaker}/{e.role}] {e.content[:600]}"
            for e in recent_dialogue
        )
        + "\n\n"
    )

    user_request_block = f"USER REQUEST (this turn): {state.user_request[:1000]}\n\n"

    updater_prompt = (
        f"{prior_block}"
        f"{contract_summary}"
        f"{user_request_block}"
        f"{dialogue_block}"
        f"Emit the FULL CURRENT state-of-the-world as a ProjectStateUpdate JSON. "
        f"Carry forward valid entries from the PRIOR JOURNAL. Resolve what's "
        f"resolved. Add what's new. Distinguish DRAFTED from SENT — when in "
        f"doubt, drafted_not_sent."
    )

    try:
        verdict_text = await _gemini_call(
            PROJECT_STATE_UPDATER_GEMINI, updater_prompt, ProjectStateUpdate
        )
        update = ProjectStateUpdate.model_validate_json(_strip_codefence(verdict_text))
        # Server-owned turn_id isn't on state; reuse a short hash of dialogue
        # length as a turn marker for the journal footer.
        turn_marker = f"d{len(state.dialogue)}"
        project_state_journal.save_journal(
            state.project_dir, update, turn_id=turn_marker
        )
        log_line = (
            f"[project_state] updated "
            f"(pending={len(update.pending)}, "
            f"drafted={len(update.drafted_not_sent)}, "
            f"confirmed={len(update.confirmed_facts)}, "
            f"open_qs={len(update.open_questions)})"
        )
    except Exception as e:  # noqa: BLE001
        # Memory write is best-effort — never fail an already-approved turn.
        log_line = f"[project_state] update failed (best-effort): {type(e).__name__}: {e}"

    return {"log": state.log + [log_line]}


# ---------------------------------------------------------------------------
# Human Fallback
# ---------------------------------------------------------------------------
def human_fallback_node(state: CothinkState) -> dict:
    print("\n" + "=" * 60)
    print("COTHINK HALTED — HUMAN FALLBACK")
    print("=" * 60)
    print(f"Reason: {state.halt_reason or 'circuit breaker tripped'}")
    print(f"Counters: {state.counters.model_dump()}")
    print("Last log lines:")
    for line in state.log[-10:]:
        print(f"  {line}")
    if state.last_verdict:
        print(f"Last verdict: {state.last_verdict.decision} — {state.last_verdict.rationale}")
    if state.replan_history:
        print(f"Replan reasons attempted: {len(state.replan_history)}")
        for i, r in enumerate(state.replan_history, 1):
            print(f"  {i}. {r[:120]}")
    if state.pre_execute_commit_hash:
        print(
            f"\nTo revert all cothink changes back to your pre-run state, run:\n"
            f"  git -C \"{state.project_dir}\" reset --hard {state.pre_execute_commit_hash}"
        )
    print("=" * 60)
    return {"halt_reason": state.halt_reason or "human_fallback"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _strip_codefence(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        first_nl = s.find("\n")
        if first_nl != -1:
            s = s[first_nl + 1 :]
        if s.endswith("```"):
            s = s[:-3]
    return s.strip()
