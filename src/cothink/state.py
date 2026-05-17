from typing import Literal, Optional
from pydantic import BaseModel, Field


class DialogueEntry(BaseModel):
    """One utterance in the dual-brain exchange.

    Captured per LLM call so the CLI / web UI can render the actual back-and-forth
    instead of only the final verdict log line.
    """
    node: Literal["discovery", "planning", "executing", "contract_review", "learnings_enforcer"]
    speaker: Literal["claude", "gemini"]
    role: Literal["explore", "propose", "critique", "merge", "verdict", "review", "enforce"]
    content: str


class LearningViolation(BaseModel):
    """One captured-LEARNING violation found in a proposed diff.

    v0.6.0 Learnings Enforcer: a semantic gate that reads _collab/LEARNINGS.md
    and checks proposed diffs against captured rules. Universal #1 pain across
    all three forensic workflow profiles: AI re-introduces bugs already
    captured in LEARNINGS because LEARNINGS is markdown the AI has to *choose*
    to consult, not enforced.
    """
    rule_name: str = Field(description="Short identifier for the violated learning rule (component/decision-fragment).")
    rule_text: str = Field(description="The rule text quoted verbatim from LEARNINGS.md.")
    file_path: str = Field(description="The file in the proposed diff where the violation appears.")
    line_evidence: str = Field(description="The exact line(s) from the proposed diff that violate the rule.")
    severity: Literal["critical", "major", "minor"] = Field(
        description="critical = ship-stopper (data loss, security, prod outage); major = bug class previously captured; minor = style/idiom drift."
    )
    suggested_fix: str = Field(description="One-line description of what the diff should do instead.")


class ProjectStateUpdate(BaseModel):
    """v0.6.5 — End-of-turn snapshot of the project's state-of-the-world.

    Solves the fragmented-Q&A fact-drift problem documented across all 3
    forensic profiles (notably SMS profile: 3-week Cellcast "drafted vs
    sent" confusion). Each turn, AFTER Contract Review APPROVE, one Gemini
    Pro call emits this schema. The harness merges into
    `<project_dir>/_collab/project_state.md`. At the next turn's Discovery,
    the journal content is prepended as PROJECT STATE context so both
    brains start grounded.

    Design notes (Gemini debate, 2026-05-16, confidence 0.95):
    - Updated END OF TURN (after Contract Review APPROVE), NEVER during
      Planning — Planning is intent, not reality. Journal must record
      verified outcomes only.
    - LLM emits the FULL CURRENT STATE of each section, not deltas. Simpler,
      idempotent, the LLM has full read context to decide what's pending vs
      resolved.
    - DOES NOT replace LEARNINGS.md (which is RULES). This is FACTS.
    - DOES NOT replace session JSONL (which is the noisy transcript). This
      is the curated state-of-the-world.
    """
    pending: list[str] = Field(
        default_factory=list,
        description="Items currently blocked on someone's reply or external action. Each entry includes WHO/WHAT we're waiting on + date the wait started. Example: 'Cellcast support reply (sent 2026-05-04; 12 days no reply)'",
    )
    drafted_not_sent: list[str] = Field(
        default_factory=list,
        description="Outbound communications drafted but NOT yet actually sent. Critical distinction — drafted is NOT sent. Example: 'Email to Mel re: validation report v4 (drafted 2026-05-15)'",
    )
    confirmed_facts: list[str] = Field(
        default_factory=list,
        description="State-of-the-world claims verified by an authoritative source. Each entry includes the source. Example: '2026-05-04: Cellcast first-contact email sent to support@ (per session 2c8f4a turn t-1234)'",
    )
    open_questions: list[str] = Field(
        default_factory=list,
        description="Decisions blocked on stakeholder input. Each entry names WHO needs to decide. Example: 'Nick: after-hours ROSIE escalation policy?'",
    )
    turn_summary: str = Field(
        default="",
        description="ONE OR TWO SENTENCES describing what happened this turn. Becomes the journal's tail-end change-log. Specific, dated, source-cited.",
    )


class LearningsVerdict(BaseModel):
    """v0.6.0 — the Learnings Enforcer node's structured output.

    response_schema-bound on Gemini 3.1 Pro so the harness routes deterministically
    on `verdict`. When `violations_found`, the orchestrator routes back to
    executing_node with `replan_reason` summarizing the violations.
    """
    verdict: Literal["no_violations", "violations_found"]
    rationale: str = Field(description="One paragraph explaining the verdict.")
    violations: list[LearningViolation] = Field(
        default_factory=list,
        description="Empty when verdict='no_violations'. One entry per distinct rule violation.",
    )


class LearningEntry(BaseModel):
    """One persisted learning from a successful cothink run.

    Pydantic-typed so the harness (not the LLM) controls markdown formatting —
    prevents format drift that would break a future compact/prune step.
    """
    component: str
    decision: str
    rule: str
    run_id: str
    timestamp: str


class Invariant(BaseModel):
    name: str
    description: str


class InvariantStatus(BaseModel):
    name: str
    status: Literal["pass", "fail", "unknown"]
    reason: str


class ProposedDiff(BaseModel):
    file_path: str
    content: str
    contract_bullet_quoted: str
    rationale: str


class Counters(BaseModel):
    mechanical_fails: int = 0
    contract_fails: int = 0
    revisions: int = 0
    replans: int = 0
    # v0.6.0: track how many times the Learnings Enforcer has caught a violation
    # in this run. Circuit-breaker caps it to avoid replan-loops if Claude keeps
    # re-introducing the same captured rule.
    learnings_fails: int = 0


class VerdictEvaluation(BaseModel):
    """v0.6.1 — Devil's Advocate evaluation.

    After Gemini emits a GeminiVerdict, Claude is forced to evaluate it
    through this schema. Universal pain across 3 forensic profiles:
    *"dont just agree okay, fight back also why cant you remember that u
    both have thinking brain think with it fight with it"* (Guidebook),
    *"analyze with gemini dual brain server dont just rubber stamp okay"*
    (SMS), TARA's CLAUDE.md hard rule: *"Genuinely debate — don't rubber-stamp."*

    Field ordering MATTERS. `identified_flaw_in_critique` is REQUIRED to
    be filled in FIRST in the JSON object so the LLM's token-probability
    bias toward agreement is broken before the decision token is sampled.
    Per the dual-brain debate that birthed this design (confidence 0.92):
    *"To prevent theatre, you must break the LLM's token-probability bias
    toward agreement."*

    Decision semantics:
      - CONCUR: Claude agrees with Gemini's verdict. Identified flaw was
        considered and judged not load-bearing. Pipeline proceeds on Gemini's verdict.
      - PUSH_BACK: Claude disagrees. Identified flaw is load-bearing. The
        orchestrator overrides Gemini's APPROVE (if any) → routes back to
        executing with the flaw as replan context.
    """
    identified_flaw_in_critique: str = Field(
        description="REQUIRED FIRST. Identify at least one potential flaw, missed edge case, or over-correction in the partner's verdict. If you genuinely see no flaw, say so explicitly with reasoning — but try hard before admitting that."
    )
    code_evidence: str = Field(
        description="Quote the EXACT lines of code, contract bullet, or invariant text that ground your flaw above. Vague claims are rubber-stamping in disguise."
    )
    flaw_severity: Literal["critical", "major", "nit", "none"] = Field(
        description=(
            "critical = ship-stopper (security, data loss, prod outage class); "
            "major = real bug class that the partner missed; "
            "nit = pedantic / stylistic / inconsistency that's worth noting but does NOT block; "
            "none = used only when identified_flaw_in_critique honestly says 'no real flaw found'. "
            "Only critical + major trigger an override of the partner's APPROVE. Use nit liberally — it gets streamed but doesn't loop the pipeline."
        )
    )
    final_decision: Literal["CONCUR", "PUSH_BACK"] = Field(
        description=(
            "CONCUR = partner's verdict stands as-is (you can still have flagged a nit). "
            "PUSH_BACK = load-bearing flaw, override required. "
            "RULE: PUSH_BACK is only valid when flaw_severity is critical or major. Nits go to CONCUR with the nit recorded."
        )
    )
    pushback_reason: Optional[str] = Field(
        default=None,
        description="If final_decision=PUSH_BACK, one-sentence summary of what the partner missed. Becomes the replan_reason context.",
    )


class GeminiVerdict(BaseModel):
    decision: Literal["APPROVE", "REVISE", "VETO"]
    rationale: str
    invariants_evaluated: list[InvariantStatus] = Field(default_factory=list)
    revision_targets: list[str] = Field(default_factory=list)
    # v0.5.4: Planning emits whether the user's request actually requires
    # code changes. When false, Executing/Mechanical Gate/Contract Review
    # are skipped — the Discovery + Planning analysis IS the response.
    # Default True so legacy callers (build_mode CLI, existing /build endpoint)
    # behave unchanged.
    build_needed: bool = True


class CothinkState(BaseModel):
    user_request: str
    # v0.6.2 — paths (relative to project_dir or absolute under _collab/images/)
    # of screenshots the user pasted with this turn. Downsampled server-side
    # to ≤1024×1024 before landing here, per Gemini's debate safeguard against
    # accelerating the 429 cliff. Discovery + Planning include these in their
    # multimodal calls to both brains.
    attached_images: list[str] = Field(default_factory=list)
    # v0.6.6 — @-mentions from the composer Context Chips. Each entry:
    # {"kind": "file" | "folder" | "symbol", "path": "<workspace-relative>",
    #  "symbol": "<optional symbol name>"}.
    # Discovery pre-reads file mentions and folds them into user_msg.
    attached_mentions: list[dict] = Field(default_factory=list)
    project_dir: str = "."

    understanding: dict = Field(default_factory=dict)
    discovery_complete: bool = False

    design_contract: list[str] = Field(default_factory=list)
    invariants: list[Invariant] = Field(default_factory=list)
    plan_approved: bool = False

    proposed_diffs: list[ProposedDiff] = Field(default_factory=list)
    last_verdict: Optional[GeminiVerdict] = None
    untested_diffs: bool = False
    last_stderr: Optional[str] = None

    mechanical_pass: bool = False
    contract_status: list[InvariantStatus] = Field(default_factory=list)

    # v0.6.0 Learnings Enforcer
    learnings_pass: bool = False
    last_learnings_verdict: Optional[LearningsVerdict] = None

    # v0.6.1 Devil's Advocate — log of every verdict-evaluation pass so the
    # webview / TUI can render the "Claude pushed back on Gemini at step X"
    # signal. The list grows append-only across the run.
    verdict_evaluations: list[VerdictEvaluation] = Field(default_factory=list)

    counters: Counters = Field(default_factory=Counters)
    halt_reason: Optional[str] = None
    replan_reason: Optional[str] = None
    replan_history: list[str] = Field(default_factory=list)
    pre_execute_checkpointed: bool = False
    pre_execute_commit_hash: Optional[str] = None

    claude_messages: list[dict] = Field(default_factory=list)
    gemini_messages: list[dict] = Field(default_factory=list)
    dialogue: list[DialogueEntry] = Field(default_factory=list)
    log: list[str] = Field(default_factory=list)
    learnings_loaded: int = 0
