"""v0.6.0 — Learnings Enforcer node.

Universal #1 pain across three forensic workflow profiles:
the AI keeps re-introducing project-specific bugs that are ALREADY CAPTURED in
`_collab/LEARNINGS.md` because LEARNINGS is markdown the AI has to *choose* to
consult — not enforced. PetLet TARA profile estimate: "would have prevented
4 of the 5 bug-fix commits in the last 14 days."

This node is a SEMANTIC gate (not regex/AST per Gemini's pushback — false
positives at 3 AM would get the harness abandoned). It runs after the
Mechanical Gate passes, before Contract Review.

Mechanism:
  1. Read `<project_dir>/_collab/LEARNINGS.md`. If missing or empty → no-op pass.
  2. Send {rules_markdown, proposed_diffs} to Gemini 3.1 Pro with
     response_schema=LearningsVerdict.
  3. If verdict == "violations_found" and severity in {critical, major}:
       route back to executing_node with replan_reason summarizing violations.
  4. Otherwise: mark learnings_pass=True and pass through to contract_review.

Locked rule (per user): both top-tier brains always. Uses Gemini 3.1 Pro,
NOT Flash, despite the suggestion in the dual-brain debate that birthed
this node. Speed is fine — this runs once per executing turn.
"""

from __future__ import annotations

import json
from pathlib import Path

from .memory import load_learnings
from .nodes import _add_dialogue, _gemini_call, _strip_codefence
from .state import (
    CothinkState,
    LearningsVerdict,
)


LEARNINGS_ENFORCER_SYSTEM = """You are the LEARNINGS ENFORCER in the cothink dual-brain orchestrator.

You have ONE job: detect violations of captured project-specific rules in a set of proposed code diffs.

INPUTS:
- LEARNINGS_MARKDOWN: the project's `_collab/LEARNINGS.md` file. Each entry captures a past mistake the team has explicitly agreed to never repeat. Format varies — could be a markdown table (| Date | Component | Decision | Rule |), or `## component — run X` sections with `**Rule:**` lines, or other free-form. Extract the RULES, ignore the surrounding narrative.
- PROPOSED_DIFFS: a list of file_path + content + rationale tuples the Executing node wants to write.

YOUR JOB:
1. Extract every RULE from LEARNINGS_MARKDOWN. A rule is any captured constraint, anti-pattern, required idiom, or "we agreed never to do X" decision. Rules typically appear after labels like "Rule:", "Decision:", or in the rightmost column of a markdown table.
2. For each proposed diff, check whether the diff VIOLATES any extracted rule. Match on intent, not exact string — `_date.today()` violates "never use date.today() on Cloud Run UTC" even though the variable is renamed.
3. Be CONSERVATIVE — false positives at 3 AM will get this gate disabled. A "violation" requires concrete line-level evidence in the diff, not vibes. If the rule is about a class of bug and you can't see clear evidence in the diff, emit NO violation.
4. Severity:
   - critical = ship-stopper class (data loss, security, prod outage, the bug has been captured because it shipped before and hurt the team)
   - major = bug class previously captured AND visible in the diff but not necessarily ship-stopping
   - minor = style/idiom drift, deferrable
5. Output STRICT JSON matching the LearningsVerdict schema. No prose outside the JSON.

If LEARNINGS_MARKDOWN is empty or contains no actionable rules, emit:
  {"verdict": "no_violations", "rationale": "No captured rules to enforce.", "violations": []}

If diffs are clean against ALL extracted rules, emit:
  {"verdict": "no_violations", "rationale": "<one paragraph on what you checked>", "violations": []}

Otherwise emit `violations_found` with one violation entry per distinct rule break.
"""


# Circuit-breaker: how many times the Enforcer can fire in one run before
# escalating to human_fallback. Matches the pattern of the other gates.
MAX_LEARNINGS_FAILS = 3


async def learnings_enforcer_node(state: CothinkState) -> dict:
    """Run after mechanical_node passes; gate before contract_review_node."""
    learnings_md, count = load_learnings(state.project_dir)

    # Fast path: no LEARNINGS file → pass through immediately. Don't burn a
    # Gemini call to confirm an empty file. Note: `count` is the cothink-format
    # header counter from memory.load_learnings — it only sees `## ...` headers
    # and won't count alternative formats (markdown tables, ad-hoc bullet lists).
    # Trust the raw text size, not the counter, for the existence check.
    if not learnings_md.strip():
        return {
            "learnings_pass": True,
            "last_learnings_verdict": LearningsVerdict(
                verdict="no_violations",
                rationale="No LEARNINGS.md or file empty.",
                violations=[],
            ),
            "log": state.log + [
                "[learnings_enforcer] no LEARNINGS.md or empty — pass-through"
            ],
        }

    diffs_payload = [
        {
            "file_path": d.file_path,
            "content": d.content[:4000] + ("\n…(truncated)" if len(d.content) > 4000 else ""),
            "rationale": d.rationale[:400],
        }
        for d in state.proposed_diffs
    ]

    prompt = (
        f"LEARNINGS_MARKDOWN ({count} captured entries from "
        f"{Path(state.project_dir).name}/_collab/LEARNINGS.md):\n\n"
        f"<learnings>\n{learnings_md[:60000]}\n</learnings>\n\n"
        f"PROPOSED_DIFFS:\n<diffs>\n{json.dumps(diffs_payload, indent=2)[:40000]}\n</diffs>\n\n"
        f"Emit a LearningsVerdict JSON object. Be conservative — false positives "
        f"will get this gate disabled by the user."
    )

    verdict_text = await _gemini_call(
        LEARNINGS_ENFORCER_SYSTEM, prompt, LearningsVerdict
    )
    dialogue = _add_dialogue(
        state.dialogue, "learnings_enforcer", "gemini", "enforce", verdict_text
    )

    try:
        verdict = LearningsVerdict.model_validate_json(_strip_codefence(verdict_text))
    except Exception as e:  # noqa: BLE001
        # Parse failure → treat as pass (do not block the build on enforcer
        # malfunction; log it loudly so we know to fix the prompt).
        return {
            "learnings_pass": True,
            "dialogue": dialogue,
            "log": state.log + [
                f"[learnings_enforcer] verdict parse error: {e}; treating as pass"
            ],
        }

    if verdict.verdict == "no_violations":
        return {
            "learnings_pass": True,
            "last_learnings_verdict": verdict,
            "dialogue": dialogue,
            "log": state.log + [
                f"[learnings_enforcer] {count} rules checked — no violations"
            ],
        }

    # Violations found. Filter to actionable severity (critical + major).
    # Minor violations log but don't block — same instinct as ruff vs mypy.
    blocking = [v for v in verdict.violations if v.severity in ("critical", "major")]
    if not blocking:
        return {
            "learnings_pass": True,
            "last_learnings_verdict": verdict,
            "dialogue": dialogue,
            "log": state.log + [
                f"[learnings_enforcer] {len(verdict.violations)} minor violations; "
                f"not blocking. Capture in LEARNINGS for next run."
            ],
        }

    # Compose a replan_reason that names rules + files so executing_node
    # has actionable context for the retry.
    violation_summary = "; ".join(
        f"{v.rule_name} at {v.file_path}: {v.suggested_fix}"
        for v in blocking[:5]
    )
    replan_reason = (
        f"learnings_enforcer caught {len(blocking)} captured-rule violation(s): "
        f"{violation_summary}"
    )

    new_counters = state.counters.model_copy(
        update={"learnings_fails": state.counters.learnings_fails + 1}
    )
    return {
        "learnings_pass": False,
        "last_learnings_verdict": verdict,
        "replan_reason": replan_reason,
        "dialogue": dialogue,
        "counters": new_counters,
        # Untested_diffs reset — Executing will rewrite, then Mechanical re-runs.
        "untested_diffs": False,
        "mechanical_pass": False,
        "log": state.log + [
            f"[learnings_enforcer] BLOCKING — {len(blocking)} violation(s): "
            f"{violation_summary[:200]}"
        ],
    }
