"""Named workflow boundaries. No placeholder node executes a business operation."""

from app.graphs.campaign.state import AgentState


class WorkflowNotImplemented(NotImplementedError):
    """Raised until a real node implementation is connected."""


def prepare_context(state: AgentState) -> AgentState:
    # TODO(context, design §§2.2, 6.3, 7, 8): load authorized task, brand
    # rules and applicable memories using server-supplied identity.
    raise WorkflowNotImplemented("prepare_context: identity, authorization and context loading are not implemented")


def interpret_event(state: AgentState) -> AgentState:
    # TODO(interpretation, design §§4.4, 6.3): typed patch/ambiguity extraction;
    # dispatch structured events without reinterpreting them through an LLM.
    raise WorkflowNotImplemented("interpret_event: event interpretation is not implemented")


def apply_event(state: AgentState) -> AgentState:
    # TODO(events, design §§4.3-4.4): apply event idempotently with provenance,
    # revisions, default suppression and explicit memory operations.
    raise WorkflowNotImplemented("apply_event: durable event application is not implemented")


def validate_and_route(state: AgentState) -> AgentState:
    # TODO(validation, design §§2.1-2.3, 6): validate business contracts,
    # clarify ambiguity and choose the next authorized stage deterministically.
    raise WorkflowNotImplemented("validate_and_route: business validation is not implemented")


def wait_for_user(state: AgentState) -> AgentState:
    # TODO(interrupt, design §§5, 6.1): persist pending UI and use LangGraph
    # interrupt/resume; reauthorize and reload state after acquiring the lease.
    raise WorkflowNotImplemented("wait_for_user: durable interrupt/resume is not implemented")


def reserve_business_operation(state: AgentState) -> AgentState:
    # TODO(operations, design §4.3): atomically reserve campaign/task request
    # and outbox; reconcile unknown outcomes before any repeat submission.
    raise WorkflowNotImplemented("reserve_business_operation: operation ledger is not implemented")


def run_recommendation(state: AgentState) -> AgentState:
    # TODO(recommendation, design §§2.5, 6): start once, persist downstream ID,
    # let worker poll and retrieve authorized candidates at terminal state.
    raise WorkflowNotImplemented("run_recommendation: async recommendation is not implemented")


def select_candidates(state: AgentState) -> AgentState:
    # TODO(candidates, design §2.3): preserve returned model suggestions; only
    # select if needed and verify every ID against the authorized full snapshot.
    raise WorkflowNotImplemented("select_candidates: result mapping is not implemented")


def wait_for_selection_confirmation(state: AgentState) -> AgentState:
    # TODO(selection, design §§2.3, 5): persist review UI and user selection;
    # require explicit final confirmation tied to current snapshot and revision.
    raise WorkflowNotImplemented("wait_for_selection_confirmation: selection review is not implemented")


def reserve_selection_confirmation(state: AgentState) -> AgentState:
    # TODO(confirmation, design §§4.3, 6.2): reauthorize, validate snapshot and
    # selection, then atomically reserve a unique confirmation and outbox item.
    raise WorkflowNotImplemented("reserve_selection_confirmation: confirmation ledger is not implemented")


def submit_confirmed_selection(state: AgentState) -> AgentState:
    # TODO(selection adapter, design §2.5): integrate the confirmed final API
    # only after business clarifies batch-select and final submission semantics.
    raise WorkflowNotImplemented("submit_confirmed_selection: final API contract is not confirmed")


def format_result(state: AgentState) -> AgentState:
    # TODO(events, design §5): project actual receipts and committed UI events.
    raise WorkflowNotImplemented("format_result: result projection is not implemented")
