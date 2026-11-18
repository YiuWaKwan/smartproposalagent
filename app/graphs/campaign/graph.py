"""Compile the reviewable workflow topology; node behavior is deliberately blocked."""

from langgraph.graph import END, START, StateGraph

from app.graphs.campaign import nodes
from app.graphs.campaign.state import AgentState


def route_after_validation(state: AgentState) -> str:
    # TODO(routing, design §6): derive route from validated business state.
    raise nodes.WorkflowNotImplemented("routing: validated business decisions are not implemented")


def build_graph():
    graph = StateGraph(AgentState)
    for name in (
        "prepare_context", "interpret_event", "apply_event", "validate_and_route",
        "wait_for_user", "reserve_business_operation", "run_recommendation",
        "select_candidates", "wait_for_selection_confirmation",
        "reserve_selection_confirmation", "submit_confirmed_selection", "format_result",
    ):
        graph.add_node(name, getattr(nodes, name))

    graph.add_edge(START, "prepare_context")
    graph.add_edge("prepare_context", "interpret_event")
    graph.add_edge("interpret_event", "apply_event")
    graph.add_edge("apply_event", "validate_and_route")
    graph.add_conditional_edges("validate_and_route", route_after_validation, {
        "wait": "wait_for_user",
        "operation": "reserve_business_operation",
        "recommendation": "run_recommendation",
        "review": "wait_for_selection_confirmation",
        "end": END,
    })
    graph.add_edge("wait_for_user", "prepare_context")
    graph.add_edge("reserve_business_operation", "run_recommendation")
    graph.add_edge("run_recommendation", "select_candidates")
    graph.add_edge("select_candidates", "wait_for_selection_confirmation")
    # TODO(selection routing, design §6): distinguish edit, confirmation and
    # cancellation; do not reserve a submission without explicit confirmation.
    graph.add_edge("wait_for_selection_confirmation", END)
    graph.add_edge("reserve_selection_confirmation", "submit_confirmed_selection")
    graph.add_edge("submit_confirmed_selection", "format_result")
    graph.add_edge("format_result", END)
    # TODO(checkpoint, design §§4.3, 6.1): inject AsyncPostgresSaver and trusted
    # runtime context after implementing the worker lease and business ledger.
    return graph.compile()
