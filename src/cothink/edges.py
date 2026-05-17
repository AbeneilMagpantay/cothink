"""Deterministic state-driven routing functions.

NO LLM calls. Each function inspects typed state and returns the next node name.
Circuit breakers route to human_fallback when counters cap out.
"""

from langgraph.graph import END

from .state import CothinkState


MAX_REVISIONS = 2
MAX_MECHANICAL_FAILS = 3
MAX_CONTRACT_FAILS = 3
MAX_REPLANS = 3
MAX_LEARNINGS_FAILS = 3


def route_after_discovery(state: CothinkState) -> str:
    if state.counters.replans >= MAX_REPLANS:
        return "human_fallback"
    if state.discovery_complete:
        return "planning"
    return "human_fallback"


def route_after_planning(state: CothinkState) -> str:
    if state.counters.replans >= MAX_REPLANS:
        return "human_fallback"
    if state.plan_approved:
        # v0.5.4: if Planning judged that no code change is needed (e.g., the
        # user's request was a question, not a build task), short-circuit
        # straight to END. Discovery + Planning IS the response — no
        # Executing/Mechanical Gate/Contract Review needed.
        if state.last_verdict is not None and not state.last_verdict.build_needed:
            return END
        return "executing"
    return "discovery"


def route_after_executing(state: CothinkState) -> str:
    if state.halt_reason and state.halt_reason.startswith("executing_veto"):
        return "human_fallback"
    if state.counters.replans >= MAX_REPLANS:
        return "human_fallback"
    if state.replan_reason:
        # Mechanism #4: Claude emitted <replan/>; route back to Planning
        # with the replan reason as context. planning_node will consume + clear it.
        return "planning"
    if state.counters.revisions > MAX_REVISIONS:
        return "human_fallback"
    if state.untested_diffs:
        return "mechanical"
    return "executing"


def route_after_mechanical(state: CothinkState) -> str:
    if state.counters.mechanical_fails >= MAX_MECHANICAL_FAILS:
        return "human_fallback"
    if state.mechanical_pass:
        # v0.6.0: Learnings Enforcer slots between Mechanical and Contract
        # Review. Deterministic project-specific anti-pattern check first;
        # general contract review after.
        return "learnings_enforcer"
    return "executing"


def route_after_learnings_enforcer(state: CothinkState) -> str:
    """v0.6.0 — gate on captured project rules.

    Circuit breaker: after MAX_LEARNINGS_FAILS the same captured rule has
    been re-violated too many times — escalate to human_fallback rather than
    burn another replan cycle. Replan back to executing is the productive
    path; replan-loop is the failure mode the breaker exists to catch.
    """
    if state.counters.learnings_fails >= MAX_LEARNINGS_FAILS:
        return "human_fallback"
    if state.learnings_pass:
        return "contract_review"
    # Violations found → re-execute with replan_reason already populated by
    # the enforcer node. Executing will rewrite the diffs, mechanical re-runs,
    # then enforcer re-checks.
    return "executing"


def route_after_contract_review(state: CothinkState) -> str:
    if state.counters.contract_fails >= MAX_CONTRACT_FAILS:
        return "human_fallback"
    all_pass = (
        state.last_verdict is not None
        and state.last_verdict.decision == "APPROVE"
        and all(s.status == "pass" for s in state.contract_status)
    )
    if all_pass:
        # v0.6.5: APPROVE now passes through project_state journal updater
        # before terminating. Failure paths still loop back to executing
        # (or human_fallback once contract_fails caps out) — only verified
        # APPROVED outcomes get recorded in the state-of-the-world journal.
        return "project_state"
    return "executing"
