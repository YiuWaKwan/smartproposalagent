"""Real PostgreSQL tests; explicitly opt in with AGENT_TEST_DATABASE_URL."""

from concurrent.futures import ThreadPoolExecutor
import asyncio
import os
from uuid import uuid4

import pytest
import psycopg

from app.persistence import ConversationRepository, Database, EventConflict
from app.schemas.business_api import ActorScope
from app.schemas.events import FormPatchEvent, UserMessage
from app.schemas.patch import FieldPatch, FormPatch
from app.graphs.campaign.graph import build_graph
from app.schemas.domain import WorkflowDraft


@pytest.fixture
def repository():
    dsn = os.getenv("AGENT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("AGENT_TEST_DATABASE_URL is not configured")
    db = Database(dsn)
    db.open()
    db.migrate()
    try:
        yield ConversationRepository(db)
    finally:
        db.close()


def test_migration_and_concurrent_deduplication(repository):
    actor = ActorScope(tenant_id="t", user_id="u", brand_id="b")
    conversation = repository.create(actor)
    event = UserMessage(client_event_id=uuid4(), task_id=conversation.workflow_id, text="hello")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(repository.accept_event, conversation.conversation_id, actor, event) for _ in range(2)]
        receipts = [future.result() for future in futures]
    assert receipts[0] == receipts[1]
    assert len(repository.messages(conversation.conversation_id, actor)) == 1
    assert len(repository.events(conversation.conversation_id, actor)) == 1
    with pytest.raises(EventConflict):
        repository.accept_event(conversation.conversation_id, actor, event.model_copy(update={"text": "different"}))
    with repository.database.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM outbox_events WHERE source_event_id=%s", (receipts[0].event_id,)).fetchone()["n"] == 1
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("UPDATE conversation_messages SET content='changed' WHERE conversation_id=%s", (conversation.conversation_id,))


def test_scope_fencing_and_outbox_reclaim(repository):
    actor = ActorScope(tenant_id="t", user_id="u", brand_id="b")
    conversation = repository.create(actor)
    with pytest.raises(PermissionError):
        repository.get(conversation.conversation_id, actor.model_copy(update={"brand_id": "other"}))
    event = UserMessage(client_event_id=uuid4(), task_id=conversation.workflow_id, text="x")
    receipt = repository.accept_event(conversation.conversation_id, actor, event)
    token = repository.claim_lease(conversation.workflow_id, "worker-a", ttl_seconds=30)
    assert token == 1
    assert repository.claim_lease(conversation.workflow_id, "worker-b") is None
    with repository.database.transaction() as conn:
        conn.execute("UPDATE workflow_leases SET lease_until=now()-interval '1 second' WHERE workflow_id=%s", (conversation.workflow_id,))
    assert repository.claim_lease(conversation.workflow_id, "worker-b") == 2
    assert not repository.renew_lease(conversation.workflow_id, "worker-a", token)
    with repository.database.transaction() as conn:
        conn.execute("UPDATE outbox_events SET available_at=now() WHERE source_event_id=%s", (receipt.event_id,))
    leases = repository.claim_outbox("worker-a", topic="workflow.event.accepted", limit=100)
    lease = next(item for item in leases if item.source_event_id == receipt.event_id)
    with repository.database.transaction() as conn:
        conn.execute("UPDATE outbox_events SET lease_until=now()-interval '1 second' WHERE outbox_id=%s", (lease.outbox_id,))
    new = next(item for item in repository.claim_outbox("worker-b", topic="workflow.event.accepted", limit=100) if item.outbox_id == lease.outbox_id)
    assert new.fencing_token == lease.fencing_token + 1
    assert not repository.finish_outbox(lease, success=True)
    assert repository.finish_outbox(new, success=True)


def test_real_postgres_checkpoint_interrupt_and_resume():
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.types import Command

    dsn = os.getenv("AGENT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("AGENT_TEST_DATABASE_URL is not configured")
    workflow_id = uuid4()
    event = FormPatchEvent(client_event_id=uuid4(), task_id=workflow_id, expected_form_revision=0,
                           patch=FormPatch(operations=[FieldPatch(target="campaign", field="product", op="set", value="one")]))
    second = FormPatchEvent(client_event_id=uuid4(), task_id=workflow_id, expected_form_revision=1,
                            patch=FormPatch(operations=[FieldPatch(target="campaign", field="persona", op="set", value="two")]))
    config = {"configurable": {"thread_id": str(workflow_id)}}

    async def scenario():
        async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
            await saver.setup()
            graph = build_graph(checkpointer=saver)
            first = await graph.ainvoke({"draft": WorkflowDraft(task_id=workflow_id), "incoming_event": event}, config)
            assert first["draft"].form_revision == 1
            assert "__interrupt__" in first
        async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
            graph = build_graph(checkpointer=saver)
            state = await graph.aget_state(config)
            assert isinstance(state.values["draft"], WorkflowDraft)
            assert isinstance(state.values["incoming_event"], FormPatchEvent)
            assert state.values["draft"].form_revision == 1
            resumed = await graph.ainvoke(Command(resume=second.model_dump(mode="json")), config)
            assert resumed["draft"].form_revision == 2
            assert resumed["draft"].campaign.persona == "two"

    asyncio.run(scenario())


def test_form_event_application_is_atomic_and_idempotent(repository):
    actor = ActorScope(tenant_id="t", user_id="u", brand_id="b")
    conversation = repository.create(actor)
    patch = FormPatchEvent(client_event_id=uuid4(), task_id=conversation.workflow_id, expected_form_revision=0,
                           patch=FormPatch(operations=[FieldPatch(target="campaign", field="product", op="set", value="A")]))
    accepted = repository.accept_event(conversation.conversation_id, actor, patch)
    token = repository.claim_lease(conversation.workflow_id, "worker")
    applied = repository.apply_form_event(conversation.conversation_id, actor, accepted.event_id, owner="worker", token=token)
    assert applied.status == "applied"
    assert repository.apply_form_event(conversation.conversation_id, actor, accepted.event_id, owner="worker", token=token) == applied
    details = repository.get(conversation.conversation_id, actor)
    assert details["workflows"][0]["form_revision"] == 1
    ui = repository.ui_events(conversation.conversation_id, actor)
    assert len(ui) == 1 and ui[0].payload["form_revision"] == 1
    with repository.database.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM outbox_events WHERE source_event_id=%s", (accepted.event_id,)).fetchone()["n"] == 2
    stale = patch.model_copy(update={"client_event_id": uuid4()})
    stale_receipt = repository.accept_event(conversation.conversation_id, actor, stale)
    conflict = repository.apply_form_event(conversation.conversation_id, actor, stale_receipt.event_id, owner="worker", token=token)
    assert conflict.status == "conflict"
    assert repository.get(conversation.conversation_id, actor)["workflows"][0]["form_revision"] == 1


def test_worker_reauthorizes_before_applying_event(repository):
    from app.workers.event_worker import EventWorker

    class Authorizer:
        allowed = False

        def authorize(self, actor):
            return self.allowed

    actor = ActorScope(tenant_id="worker-tenant", user_id="worker-user", brand_id="worker-brand")
    conversation = repository.create(actor)
    patch = FormPatchEvent(client_event_id=uuid4(), task_id=conversation.workflow_id, expected_form_revision=0,
                           patch=FormPatch(operations=[FieldPatch(target="campaign", field="product", op="set", value="B")]))
    receipt = repository.accept_event(conversation.conversation_id, actor, patch)
    authorizer = Authorizer()
    worker = EventWorker(repository, authorizer, "authorization-test-worker")
    worker.run_once()
    assert repository.get(conversation.conversation_id, actor)["workflows"][0]["form_revision"] == 0
    with repository.database.transaction() as conn:
        conn.execute("UPDATE outbox_events SET available_at=now() WHERE source_event_id=%s", (receipt.event_id,))
    authorizer.allowed = True
    worker.run_once()
    assert repository.get(conversation.conversation_id, actor)["workflows"][0]["form_revision"] == 1


def test_stream_replays_committed_ui_and_stops_on_revoked_permission(repository):
    from app.services.sse import replay_cursor, stream_ui_events

    class Auth:
        allowed = True

        def authorize(self, actor):
            return self.allowed

    actor = ActorScope(tenant_id="stream-tenant", user_id="u", brand_id="b")
    conversation = repository.create(actor)
    patch = FormPatchEvent(client_event_id=uuid4(), task_id=conversation.workflow_id, expected_form_revision=0,
                           patch=FormPatch(operations=[FieldPatch(target="campaign", field="product", op="set", value="C")]))
    receipt = repository.accept_event(conversation.conversation_id, actor, patch)
    token = repository.claim_lease(conversation.workflow_id, "sse-worker")
    repository.apply_form_event(conversation.conversation_id, actor, receipt.event_id, owner="sse-worker", token=token)
    event = repository.ui_events(conversation.conversation_id, actor)[0]
    assert replay_cursor(str(event.seq), event.seq) == event.seq
    with pytest.raises(ValueError, match="conflicting"):
        replay_cursor(str(event.seq), event.seq + 1)

    async def scenario():
        auth = Auth()
        stream = stream_ui_events(repository, auth, conversation.conversation_id, actor, after=0)
        first = await anext(stream)
        assert f"id: {event.seq}\n" in first and "event: form_snapshot" in first
        auth.allowed = False
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        await stream.aclose()
        heartbeat = stream_ui_events(repository, Auth(), conversation.conversation_id, actor, after=event.seq)
        assert await anext(heartbeat) == ": heartbeat\n\n"
        await heartbeat.aclose()

    asyncio.run(scenario())


def test_postgres_snapshot_reference_roundtrip_and_scope(repository):
    from app.persistence.snapshots import PostgresRecommendationSnapshotStore
    from app.schemas.recommendation import JavaRecommendationResults, RecommendationRun
    from app.services.recommendations import validate_results_and_make_snapshot

    actor = ActorScope(tenant_id="snapshot-tenant", user_id="u", brand_id="b")
    conversation = repository.create(actor)
    with repository.database.transaction() as conn:
        conn.execute("UPDATE workflows SET business_campaign_id=36,business_task_id=55,downstream_task_id='down' WHERE workflow_id=%s", (conversation.workflow_id,))
    run = RecommendationRun(run_id=uuid4(), campaign_id=36, business_task_id=55,
        agent_task_id=conversation.workflow_id, operation_id=uuid4(), request_id=uuid4(),
        idempotency_key=uuid4(), form_revision=0, payload_hash="a" * 64, canonical_payload="{}",
        actor_tenant_id=actor.tenant_id, actor_user_id=actor.user_id, actor_brand_id=actor.brand_id,
        confirmation_id=uuid4(), status="recommend_completed", downstream_task_id="down")
    results = JavaRecommendationResults(kols=[{"kolBackendId": "001", "source": "recommend"}])
    _, reference, _ = validate_results_and_make_snapshot(run, results)
    store = PostgresRecommendationSnapshotStore(repository.database, conversation.workflow_id, actor)
    store.put(reference, results)
    assert store.get_reference(reference.snapshot_ref) == reference
    assert store.get_results(reference.snapshot_ref) == results
    other = PostgresRecommendationSnapshotStore(repository.database, conversation.workflow_id, actor.model_copy(update={"brand_id": "other"}))
    assert other.get_results(reference.snapshot_ref) is None
    with pytest.raises(PermissionError):
        other.put(reference, results)


def test_form_transaction_rolls_back_and_lost_lease_rejects_write(repository):
    actor = ActorScope(tenant_id="rollback-tenant", user_id="u", brand_id="b")
    conversation = repository.create(actor)
    patch = FormPatchEvent(client_event_id=uuid4(), task_id=conversation.workflow_id, expected_form_revision=0,
                           patch=FormPatch(operations=[FieldPatch(target="campaign", field="product", op="set", value="D")]))
    receipt = repository.accept_event(conversation.conversation_id, actor, patch)
    token = repository.claim_lease(conversation.workflow_id, "old")
    with repository.database.transaction() as conn:
        conn.execute("UPDATE workflow_leases SET lease_until=now()-interval '1 second' WHERE workflow_id=%s", (conversation.workflow_id,))
    new_token = repository.claim_lease(conversation.workflow_id, "new")
    assert new_token > token
    with pytest.raises(RuntimeError, match="lease lost"):
        repository.apply_form_event(conversation.conversation_id, actor, receipt.event_id, owner="old", token=token)
    assert repository.get(conversation.conversation_id, actor)["workflows"][0]["form_revision"] == 0
    assert repository.ui_events(conversation.conversation_id, actor) == []
    assert repository.events(conversation.conversation_id, actor)[0]["status"] == "accepted"
    assert repository.apply_form_event(conversation.conversation_id, actor, receipt.event_id, owner="new", token=new_token).status == "applied"


def test_authorized_http_boundary_and_revocation(repository):
    from fastapi.testclient import TestClient
    from app.main import create_app

    actor = ActorScope(tenant_id="api-tenant", user_id="api-user", brand_id="api-brand")

    class Authenticator:
        async def authenticate(self, token):
            if token != "test-credential":
                raise ValueError("invalid")
            return actor

    class Authorizer:
        allowed = True

        def authorize(self, requested):
            return self.allowed and requested == actor

    app = create_app()
    auth = Authorizer()
    with TestClient(app) as client:
        assert client.get("/api/agent/conversations").status_code == 503
        app.state.conversation_repository = repository
        app.state.actor_authenticator = Authenticator()
        app.state.brand_authorizer = auth
        assert client.get("/api/agent/conversations").status_code == 401
        headers = {"Authorization": "Bearer test-credential"}
        created = client.post("/api/agent/conversations", json={"title": "Test"}, headers=headers)
        assert created.status_code == 201
        conversation_id = created.json()["conversation_id"]
        assert client.get(f"/api/agent/conversations/{conversation_id}", headers=headers).status_code == 200
        auth.allowed = False
        assert client.get(f"/api/agent/conversations/{conversation_id}", headers=headers).status_code == 403


def test_message_archive_and_summary_watermark(repository):
    actor = ActorScope(tenant_id="summary-tenant", user_id="u", brand_id="b")
    ref = repository.create(actor)
    first = repository.append_message(ref.conversation_id, actor, workflow_id=ref.workflow_id, role="user", content="untrusted text")
    second = repository.append_message(ref.conversation_id, actor, workflow_id=ref.workflow_id, role="assistant", content="response")
    summary = repository.save_summary(ref.conversation_id, actor, ref.workflow_id, second.message_id,
                                      [first.message_id, second.message_id], {"unresolved": ["budget"]})
    assert summary.covered_until_message_id == second.message_id
    assert repository.save_summary(ref.conversation_id, actor, ref.workflow_id, second.message_id,
                                   [first.message_id, second.message_id], {"unresolved": ["budget"]}) == summary
    with pytest.raises(EventConflict):
        repository.save_summary(ref.conversation_id, actor, ref.workflow_id, second.message_id,
                                [first.message_id, second.message_id], {"unresolved": []})
    assert [m.seq for m in repository.messages(ref.conversation_id, actor, after=1)] == [2]
