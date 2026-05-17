# cothink

Dual-brain orchestrator. Claude is the actor (writes code). Gemini is the navigator (debates, critiques, vetoes). They think together on every task; the harness enforces it so neither can skip the other.

## Architecture

Five-node LangGraph pipeline with deterministic state-driven routing and circuit breakers:

```
Discovery → Planning → Executing → Mechanical Gate → Contract Review → Done
                ▲           ▲              │                  │
                │           │              ↓ fail             ↓ fail
                │           └─────────── Executing            └→ Planning
                │
                └── replan with git checkpoint
                        (counter cap → Human Fallback)
```

Each node has its own tool set and actor:

- **Discovery** – read-only. Both LLMs explore the codebase and request. Output: shared mental model.
- **Planning** – read-only + structured output. Symmetric debate. Output: bounded design contract (≤50 bullets) with `[INVARIANT]` tags.
- **Executing** – read + write. Driver/Navigator. Claude proposes diffs (must quote contract bullet); Gemini emits Pydantic verdict.
- **Mechanical Gate** – shell only, no LLM. Runs `py_compile` (and lint/type if installed). Failure routes back to Executing with stderr.
- **Contract Review** – Gemini reads contract bullet-by-bullet against the delivered code. Failure routes back to Planning or Executing.
- **Human Fallback** – escape valve when any counter caps out (mechanical_fails ≥ 3, contract_fails ≥ 3, replans ≥ 3).

## Install

```bash
cd cothink
pip install -e .
cp .env.example .env
# fill in ANTHROPIC_API_KEY and GEMINI_API_KEY in .env
```

## Run

```bash
cothink "Write a Python function fibonacci(n) with edge case handling" --project-dir .
```

Each LangGraph node prints its name, log line, and the actual Claude/Gemini exchanges it generated (so you can read what each model said, not just the verdict).

## v0 scope

Active mechanisms: contract active-quoting (#1), schema-enforced invariants (#2), mechanical hard veto (#6), claim provenance via prompts (#7).

Deferred to v0.1: git auto-checkpoint on replan (#4), volume-triggered alignment (#3), MAX_REVISIONS escalation polish (#5).

Deferred to v0.2: Rich split-pane TUI.
