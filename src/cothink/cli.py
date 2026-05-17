"""CLI entry point: cothink "task" --project-dir <path>"""

import argparse
import asyncio
import os
import sys
import textwrap
import uuid
from pathlib import Path

from dotenv import load_dotenv

from .graph import build_graph
from .state import CothinkState

# Per-utterance truncation guard so the terminal isn't drowned by a 20K-char
# Claude essay. Full content is always available in state.dialogue.
_DIALOGUE_TRUNCATE_AT = 2000

# src/cothink/cli.py → src/cothink → src → <package root holding .env>
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent


def _check_env() -> None:
    if not os.environ.get("GEMINI_API_KEY"):
        sys.stderr.write(
            "cothink: missing env var GEMINI_API_KEY\n"
            "Copy .env.example to .env and fill it in.\n"
        )
        sys.exit(2)
    # Force Claude calls through claude-agent-sdk's subscription auth.
    # Any ANTHROPIC_API_KEY in the environment would otherwise route Claude Code
    # through pay-per-token API billing instead of the user's Claude subscription.
    os.environ.pop("ANTHROPIC_API_KEY", None)


def _check_clean_tree(project_dir: str) -> None:
    """Refuse to run when project_dir is a dirty git repo.

    Rationale: cothink's checkpoint commits use `git add -A`, which would
    silently snapshot the user's unrelated WIP into a commit alongside any
    cothink-generated changes. Easier to require a clean start than to try
    to surgically separate cothink's writes from the user's WIP afterward.
    """
    from .nodes import _git_is_dirty
    is_dirty, status = _git_is_dirty(project_dir)
    if not is_dirty:
        return
    sys.stderr.write(
        f"cothink: working tree at '{project_dir}' has uncommitted changes.\n"
        f"Commit or stash them before running cothink — its git checkpoints\n"
        f"would otherwise mix your WIP into cothink's commit history.\n\n"
        f"Untracked / modified files:\n{status}\n"
    )
    sys.exit(3)


def _print_new_dialogue(node_state: dict, seen: int) -> int:
    """Print DialogueEntries from `node_state['dialogue']` after index `seen`.

    Returns the new total dialogue length so the caller can update its watermark.
    Each utterance is rendered as a [SPEAKER · role] header followed by indented
    content, truncated to _DIALOGUE_TRUNCATE_AT chars per utterance.
    """
    dialogue = node_state.get("dialogue") or []
    new_entries = dialogue[seen:]
    for entry in new_entries:
        e = entry if isinstance(entry, dict) else entry.model_dump()
        speaker = (e.get("speaker") or "?").upper()
        role = e.get("role") or "?"
        content = e.get("content") or ""
        full_len = len(content)
        if full_len > _DIALOGUE_TRUNCATE_AT:
            content = (
                content[:_DIALOGUE_TRUNCATE_AT]
                + f"\n... (truncated, {full_len - _DIALOGUE_TRUNCATE_AT} more chars in state.dialogue)"
            )
        print(f"\n  [{speaker} · {role}]")
        print(textwrap.indent(content, "    "))
    return len(dialogue)


async def run(task: str, project_dir: str) -> int:
    _check_env()
    resolved_project_dir = str(Path(project_dir).resolve())
    _check_clean_tree(resolved_project_dir)
    graph = build_graph()
    initial = CothinkState(user_request=task, project_dir=resolved_project_dir)
    config = {"configurable": {"thread_id": str(uuid.uuid4())}, "recursion_limit": 50}

    final_state: dict = {}
    seen_dialogue = 0
    async for event in graph.astream(initial, config=config):
        for node_name, node_state in event.items():
            print(f"\n=== {node_name} ===")
            if isinstance(node_state, dict):
                if "log" in node_state and node_state["log"]:
                    print(node_state["log"][-1])
                seen_dialogue = _print_new_dialogue(node_state, seen_dialogue)
                final_state = node_state

    print("\n" + "=" * 60)
    print("COTHINK FINISHED")
    print("=" * 60)
    if isinstance(final_state, dict):
        halt = final_state.get("halt_reason")
        if halt:
            print(f"Halted: {halt}")
            return 1
    print("Done.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="cothink", description="Dual-brain orchestrator")
    parser.add_argument(
        "task",
        nargs="?",
        help="Build-mode task (omit when using --chat)",
    )
    parser.add_argument("--project-dir", default=".", help="Project directory (default: cwd)")
    parser.add_argument(
        "--chat",
        action="store_true",
        help="Open the interactive debate REPL instead of running a build",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Launch the HTTP/SSE server on 127.0.0.1:8765 (consumed by the VSCode extension)",
    )
    args = parser.parse_args()

    env_path = _PACKAGE_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path)
    else:
        load_dotenv()

    if args.serve:
        from .server import run_server
        run_server()
        return

    if args.chat:
        from .chat import chat_loop
        _check_env()
        resolved = str(Path(args.project_dir).resolve())
        _check_clean_tree(resolved)
        rc = asyncio.run(chat_loop(resolved))
        sys.exit(rc)

    if not args.task:
        parser.error("provide a task, pass --chat for the interactive REPL, or --serve to launch the HTTP bridge")

    rc = asyncio.run(run(args.task, args.project_dir))
    sys.exit(rc)


if __name__ == "__main__":
    main()
