"""Assemble the LangGraph application."""

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

from .state import CothinkState
from .nodes import (
    discovery_node,
    planning_node,
    executing_node,
    mechanical_node,
    contract_review_node,
    human_fallback_node,
    project_state_node,
)
from .learnings_enforcer import learnings_enforcer_node
from .edges import (
    route_after_discovery,
    route_after_planning,
    route_after_executing,
    route_after_mechanical,
    route_after_learnings_enforcer,
    route_after_contract_review,
)


def build_graph():
    builder = StateGraph(CothinkState)

    builder.add_node("discovery", discovery_node)
    builder.add_node("planning", planning_node)
    builder.add_node("executing", executing_node)
    builder.add_node("mechanical", mechanical_node)
    builder.add_node("learnings_enforcer", learnings_enforcer_node)
    builder.add_node("contract_review", contract_review_node)
    builder.add_node("project_state", project_state_node)  # v0.6.5
    builder.add_node("human_fallback", human_fallback_node)

    builder.add_edge(START, "discovery")
    builder.add_conditional_edges("discovery", route_after_discovery)
    builder.add_conditional_edges("planning", route_after_planning)
    builder.add_conditional_edges("executing", route_after_executing)
    builder.add_conditional_edges("mechanical", route_after_mechanical)
    builder.add_conditional_edges("learnings_enforcer", route_after_learnings_enforcer)
    # v0.6.5: contract_review APPROVE now routes through project_state for
    # journal update (instead of straight to END). Failure paths from
    # route_after_contract_review (executing/human_fallback) skip the journal.
    builder.add_conditional_edges("contract_review", route_after_contract_review)
    builder.add_edge("project_state", END)

    return builder.compile(checkpointer=MemorySaver())
