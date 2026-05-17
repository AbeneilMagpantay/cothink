"""cothink chat — interactive debate-mode REPL.

Each user prompt triggers a multi-round debate between Claude and Gemini.
No hard round cap; either model can end the debate unilaterally by emitting
the exact phrase 'I think we've covered this.' The anti-sycophancy system
prompt is engineered to make that signal mean something (each round must
add substance OR stop, no filler).

Slash commands:
  /build <task>   → escape into the structured 5-node integrity pipeline
  /reset          → clear the conversation history (keep the session open)
  /quit, /exit    → exit cleanly
  Ctrl-C          → interrupt the current debate round, keep history,
                    return to the input prompt
  Ctrl-D / EOF    → exit cleanly

UI: rich.Live panels stream tokens as they arrive. Claude rounds cyan,
Gemini rounds magenta, system messages dim. Markdown rendered inline so
code blocks get syntax highlighting.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel

from .nodes import _claude_stream, _gemini_stream


STOP_PHRASE = "I think we've covered this."

DEBATER_SYSTEM = """You are in a debate with the other LLM on the user's question.

Your job each turn:
(a) If you have a SUBSTANTIVE new point or genuine pushback — say it directly,
    no agreement preamble.
(b) If the topic is genuinely resolved — output the EXACT phrase:
        I think we've covered this.
    and NOTHING else. Not as preamble, not as suffix — only that line.
(c) If you're tempted to add "good point, and also..." without substance —
    that means option (b). Take option (b).

Do NOT acknowledge the other model's point as preamble. Do NOT add filler.
Either contribute or stop.

You will receive the user's question followed by the prior debate rounds
(if any) in the format:
    ## USER
    <question>
    ## ROUND 1 · CLAUDE
    <text>
    ## ROUND 2 · GEMINI
    <text>
    ...

Your reply IS the next round. Write only your reply (or the stop phrase).
"""


def _build_debate_prompt(history: list[dict[str, Any]]) -> str:
    """Serialize the running conversation + user's current question for the next round."""
    parts: list[str] = []
    for entry in history:
        role = entry["role"]
        if role == "user":
            parts.append(f"## USER\n{entry['content']}\n")
        else:
            speaker = entry.get("speaker", "?").upper()
            round_num = entry.get("round", "?")
            parts.append(f"## ROUND {round_num} · {speaker}\n{entry['content']}\n")
    return "\n".join(parts)


def _is_stop_signal(text: str) -> bool:
    """The model signals end-of-debate by emitting exactly the STOP_PHRASE.

    Tolerant of trailing whitespace and a final period variant, but not of
    sycophantic preamble — the whole point is that a model with nothing to
    add must produce ONLY the phrase, no commentary.
    """
    cleaned = text.strip().rstrip(".").strip()
    return cleaned.lower() == STOP_PHRASE.rstrip(".").lower()


async def _run_round(
    console: Console,
    history: list[dict[str, Any]],
    round_num: int,
) -> tuple[str, bool]:
    """Run one round of the debate. Returns (text, should_continue).

    Odd rounds → Claude; even rounds → Gemini. Streams tokens to a live
    rich.Panel as they arrive.
    """
    is_claude = round_num % 2 == 1
    speaker = "CLAUDE" if is_claude else "GEMINI"
    color = "cyan" if is_claude else "magenta"
    stream_fn = _claude_stream if is_claude else _gemini_stream

    user_prompt = _build_debate_prompt(history)
    chunks: list[str] = []

    title = f"[{color} bold]ROUND {round_num} · {speaker}[/]"
    panel = Panel("", title=title, border_style=color)

    try:
        with Live(panel, console=console, refresh_per_second=12) as live:
            async for chunk in stream_fn(DEBATER_SYSTEM, user_prompt):
                chunks.append(chunk)
                body = "".join(chunks)
                live.update(
                    Panel(
                        Markdown(body) if body.strip() else "",
                        title=title,
                        border_style=color,
                    )
                )
    except KeyboardInterrupt:
        # Surface partial content visibly so user sees what happened, then re-raise.
        console.print(
            f"[yellow]Round {round_num} interrupted (Ctrl-C); discarding partial output.[/]"
        )
        raise

    text = "".join(chunks).strip()
    return text, not _is_stop_signal(text)


async def _run_debate(console: Console, history: list[dict[str, Any]]) -> None:
    """Run rounds until a model emits the stop phrase or user interrupts."""
    round_num = 0
    try:
        while True:
            round_num += 1
            is_claude = round_num % 2 == 1
            speaker = "CLAUDE" if is_claude else "GEMINI"

            text, should_continue = await _run_round(console, history, round_num)

            if _is_stop_signal(text):
                history.append(
                    {
                        "role": "assistant",
                        "speaker": speaker.lower(),
                        "round": round_num,
                        "content": STOP_PHRASE,
                    }
                )
                console.print(f"[dim]({speaker} ended the debate; {round_num} rounds total)[/]")
                return

            history.append(
                {
                    "role": "assistant",
                    "speaker": speaker.lower(),
                    "round": round_num,
                    "content": text,
                }
            )

            # Soft progress hint every 10 rounds — no cap, just visibility.
            if round_num % 10 == 0:
                console.print(
                    f"[dim]--- {round_num} rounds, debate still active; Ctrl-C to interrupt ---[/]"
                )
    except KeyboardInterrupt:
        console.print(
            f"[yellow]Debate interrupted at round {round_num}. "
            f"Completed rounds preserved in history.[/]"
        )


async def _run_build_escape(console: Console, task: str, project_dir: str) -> None:
    """Dispatch into the existing 5-node integrity pipeline from inside chat."""
    from .graph import build_graph
    from .state import CothinkState

    console.print(
        Panel(
            f"[bold]Build mode[/] — running 5-node integrity pipeline\n[dim]task:[/] {task}",
            border_style="yellow",
        )
    )

    graph = build_graph()
    initial = CothinkState(user_request=task, project_dir=project_dir)
    config = {"configurable": {"thread_id": str(uuid.uuid4())}, "recursion_limit": 50}

    async for event in graph.astream(initial, config=config):
        for node_name, node_state in event.items():
            log_line = ""
            if isinstance(node_state, dict):
                log = node_state.get("log") or []
                log_line = log[-1] if log else ""
            console.print(f"[yellow]=== {node_name} ===[/] {log_line}")

    console.print(Panel("[bold]Build complete[/] — returning to chat", border_style="yellow"))


def _print_banner(console: Console) -> None:
    console.print(
        Panel(
            "[bold]cothink chat[/] — debate-mode REPL\n"
            "Each prompt triggers a back-and-forth between Claude and Gemini.\n"
            "No round cap; either model can end with 'I think we've covered this.'\n\n"
            "[dim]/build <task>[/]   run the structured 5-node integrity pipeline\n"
            "[dim]/reset[/]          clear conversation history\n"
            "[dim]/quit, /exit, Ctrl-D[/]   exit",
            border_style="blue",
        )
    )


async def chat_loop(project_dir: str) -> int:
    """Main interactive loop. Returns process exit code."""
    console = Console()
    _print_banner(console)
    history: list[dict[str, Any]] = []

    while True:
        try:
            user_input = await asyncio.to_thread(input, "\n> ")
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]exit[/]")
            return 0

        line = user_input.strip()
        if not line:
            continue

        # Slash commands
        if line in ("/quit", "/exit"):
            return 0
        if line == "/reset":
            history = []
            console.print("[dim]history reset[/]")
            continue
        if line.startswith("/build "):
            task = line[len("/build "):].strip()
            if not task:
                console.print("[yellow]usage: /build <task description>[/]")
                continue
            await _run_build_escape(console, task, project_dir)
            continue
        if line.startswith("/"):
            console.print(f"[yellow]unknown command: {line.split()[0]}[/]")
            continue

        # Regular debate turn
        history.append({"role": "user", "content": line})
        debate_start = len(history)  # marker so we can compress after the round
        await _run_debate(console, history)
        _compress_debate_history(history, debate_start, console)


def _compress_debate_history(
    history: list[dict[str, Any]], debate_start: int, console: Console
) -> None:
    """Collapse a finished debate's rounds into a single summary entry.

    Context-rot mitigation (cothink v0.4): if every debate left N round
    entries in history, the next user prompt sees the full transcript of
    every prior debate, and the LLMs degrade. We instead keep the user's
    question + the FINAL substantive round (the resolution) for context,
    dropping the back-and-forth that led there.

    The full transcript is still printed live during the debate — the user
    saw it. We just don't carry every word of it forward in the LLM's prompt
    history. The DR-driven harness in Step 3 will replace this with a more
    sophisticated tiered-memory approach.
    """
    debate_entries = history[debate_start:]
    if not debate_entries:
        return

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
        history[debate_start:] = [summary]
    else:
        history[debate_start:] = [
            {
                "role": "assistant",
                "speaker": "team",
                "round": 0,
                "content": "(debate yielded no substantive content)",
            }
        ]
    console.print(
        f"[dim]history compressed: {len(debate_entries)} rounds → 1 summary "
        f"(prevents context rot across prompts)[/]"
    )
