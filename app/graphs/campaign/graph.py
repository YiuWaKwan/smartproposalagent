"""Compile the campaign, recommendation and local selection workflow."""

from langgraph.graph import END, START, StateGraph

from app.graphs.campaign import nodes
from app.graphs.campaign.state import AgentState
from app.schemas.events import ConfirmSelection, SelectionPatchEvent, SelectionReviewRequested, SelectionSaveRequested
from app.schemas.recommendation import (
    RecommendationOperationConfirmation,
    RecommendationPollEvent,
    RecommendationStartRequested,
)
from app.services.contracts import BusinessServices, RecommendationSnapshotStore
from app.services.recommendation_snapshots import VolatileRecommendationSnapshotStore


def route_after_validation(state: AgentState) -> str:
    draft = state["draft"]
    if draft.next_action == "wait_for_user":
        return "wait"
    if draft.next_action == "wait_for_operation_confirmation":
        return "preview"
    if draft.next_action == "wait_for_recommendation_confirmation":
        return "recommend_preview"
    if draft.next_action == "reserve_business_operation":
        operation = draft.pending_business_operation
        if operation and operation.operation_type.value == "start_recommendation":
            return "end"
        return "operation"
    return "end"


def _operation_node(business_services: BusinessServices | None):
    async def execute(state: AgentState) -> AgentState:
        return await nodes.reserve_business_operation(
            state, business_services=business_services
        )

    return execute


def _recommendation_node(business_services: BusinessServices | None, snapshot_store):
    async def execute(state: AgentState) -> AgentState:
        return await nodes.execute_recommendation_operation(
            state, business_services=business_services, snapshot_store=snapshot_store
        )

    return execute


def _recommendation_preview_node(business_services: BusinessServices | None):
    async def execute(state: AgentState) -> AgentState:
        return await nodes.prepare_recommendation_preview(
            state, business_services=business_services
        )

    return execute


def _event_route(state: AgentState) -> str:
    event = state.get("incoming_event")
    if isinstance(event, RecommendationStartRequested):
        return "recommendation_preview"
    if isinstance(event, RecommendationOperationConfirmation):
        return "recommendation_confirm"
    if isinstance(event, RecommendationPollEvent):
        return "recommendation_poll"
    if isinstance(event, SelectionReviewRequested):
        return "selection_review"
    if isinstance(event, SelectionPatchEvent):
        return "selection_patch"
    if isinstance(event, SelectionSaveRequested):
        return "selection_save"
    if isinstance(event, ConfirmSelection):
        return "selection_confirm"
    return "business"


def build_graph(
    business_services: BusinessServices | None = None,
    snapshot_store: RecommendationSnapshotStore | None = None,
    checkpointer=None,
):
    if snapshot_store is None:
        snapshot_store = VolatileRecommendationSnapshotStore()
    graph = StateGraph(AgentState)
    for name in (
        "prepare_context", "interpret_event", "apply_event",
        "validate_and_route", "prepare_operation_preview", "prepare_recommendation_preview",
        "wait_for_user", "reserve_business_operation", "execute_recommendation_operation",
    ):
        if name == "reserve_business_operation":
            graph.add_node(name, _operation_node(business_services))
        elif name == "execute_recommendation_operation":
            graph.add_node(name, _recommendation_node(business_services, snapshot_store))
        elif name == "prepare_recommendation_preview":
            graph.add_node(name, _recommendation_preview_node(business_services))
        else:
            graph.add_node(name, getattr(nodes, name))

    def handle_selection(state: AgentState) -> AgentState:
        return nodes.handle_selection_event(state, snapshot_store=snapshot_store)

    selection_routes = {
        "selection_review": "wait_for_selection_confirmation",
        "selection_patch": "edit_selection_draft",
        "selection_save": "check_selection_save",
        "selection_confirm": "check_final_selection_confirmation",
    }
    for name in selection_routes.values():
        graph.add_node(name, handle_selection)
        graph.add_edge(name, END)

    graph.add_edge(START, "prepare_context")
    graph.add_edge("prepare_context", "interpret_event")
    graph.add_conditional_edges("interpret_event", _event_route, {
        "recommendation_preview": "prepare_recommendation_preview",
        "recommendation_confirm": "execute_recommendation_operation",
        "recommendation_poll": "execute_recommendation_operation",
        **selection_routes,
        "business": "apply_event",
    })
    graph.add_edge("apply_event", "validate_and_route")
    graph.add_conditional_edges("validate_and_route", route_after_validation, {
        "wait": "wait_for_user",
        "preview": "prepare_operation_preview",
        "recommend_preview": "prepare_recommendation_preview",
        "operation": "reserve_business_operation",
        "end": END,
    })
    graph.add_edge("prepare_operation_preview", END)
    graph.add_edge("prepare_recommendation_preview", END)
    graph.add_edge("wait_for_user", "prepare_context")
    graph.add_edge("reserve_business_operation", END)
    graph.add_edge("execute_recommendation_operation", END)
    return graph.compile(checkpointer=checkpointer)
