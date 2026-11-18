from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.graphs.campaign.nodes import WorkflowNotImplemented
from app.main import app
from app.schemas.domain import WorkflowDraft
from app.schemas.events import ConfirmSelection


def test_health_and_main_graph_chain() -> None:
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok", "workflow": "not_implemented"}
        graph = client.app.state.workflow.graph
        assert "wait_for_selection_confirmation" in graph.get_graph().nodes
        with pytest.raises(WorkflowNotImplemented, match="prepare_context"):
            graph.invoke({"draft": WorkflowDraft(task_id=uuid4())})


def test_selection_confirmation_requires_bound_ids_and_revision() -> None:
    with pytest.raises(ValidationError):
        ConfirmSelection(client_event_id=uuid4(), task_id=uuid4())


def test_draft_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        WorkflowDraft(task_id=uuid4(), tenant_id="untrusted")
