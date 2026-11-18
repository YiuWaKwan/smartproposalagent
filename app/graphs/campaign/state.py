"""Checkpointable graph state; dependencies and credentials stay outside it."""

from typing import TypedDict

from app.schemas.domain import WorkflowDraft
from app.schemas.events import ConfirmSelection, UserMessage


class AgentState(TypedDict, total=False):
    schema_version: int
    graph_version: int
    draft: WorkflowDraft
    incoming_event: UserMessage | ConfirmSelection
    applied_event_id: str | None
    pending_ui_ref: str | None
    request_id: str | None
    candidate_snapshot_ref: str | None
    result_ref: str | None


# TODO(state, design §§4, 6): add typed provenance, suppressed defaults,
# validation, summary, memory references, operation and confirmation records.
# Confirm the selected serializer and schema migration with the Postgres saver.
