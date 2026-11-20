"""Transactional conversation/event/outbox repository with database uniqueness."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Any
from uuid import UUID, uuid4

from psycopg import Connection

from app.persistence.database import Database
from app.persistence.models import ConversationRef, EventReceipt, MessageRecord, OutboxLease, UIEvent
from app.persistence.models import ContextSummary
from app.schemas.business_api import ActorScope
from app.schemas.domain import WorkflowDraft
from app.schemas.events import AgentEvent, UserMessage


def canonical_event_hash(event: AgentEvent) -> str:
    encoded = json.dumps(event.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


class EventConflict(ValueError):
    pass


class ConversationRepository:
    def __init__(self, database: Database):
        self.database = database

    def create(self, actor: ActorScope, *, conversation_id: UUID | None = None, workflow_id: UUID | None = None,
               title: str | None = None) -> ConversationRef:
        conversation_id = conversation_id or uuid4()
        workflow_id = workflow_id or uuid4()
        thread_id = f"agent:{conversation_id}:{workflow_id}"
        draft = WorkflowDraft(task_id=workflow_id)
        with self.database.transaction() as conn:
            conn.execute("INSERT INTO conversations(conversation_id,tenant_id,owner_user_id,brand_id,thread_id,title,active_workflow_id) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                         (conversation_id, actor.tenant_id, actor.user_id, actor.brand_id, thread_id, title, workflow_id))
            conn.execute("INSERT INTO workflows(workflow_id,conversation_id,tenant_id,owner_user_id,brand_id,draft_json) VALUES (%s,%s,%s,%s,%s,%s)",
                         (workflow_id, conversation_id, actor.tenant_id, actor.user_id, actor.brand_id, json.dumps(draft.model_dump(mode="json"))))
        return ConversationRef(conversation_id=conversation_id, workflow_id=workflow_id, thread_id=thread_id,
                               actor_scope=actor, brand_id=actor.brand_id)

    def _authorize_conversation(self, conn: Connection, conversation_id: UUID, actor: ActorScope, *, lock: bool = False):
        sql = "SELECT * FROM conversations WHERE conversation_id=%s"
        if lock:
            sql += " FOR UPDATE"
        row = conn.execute(sql, (conversation_id,)).fetchone()
        if row is None or (row["tenant_id"], row["owner_user_id"], row["brand_id"]) != (actor.tenant_id, actor.user_id, actor.brand_id):
            raise PermissionError("conversation is unavailable for the current actor and brand")
        return row

    def get(self, conversation_id: UUID, actor: ActorScope) -> dict[str, Any]:
        with self.database.connection() as conn:
            conversation = self._authorize_conversation(conn, conversation_id, actor)
            workflows = conn.execute("SELECT * FROM workflows WHERE conversation_id=%s ORDER BY updated_at DESC", (conversation_id,)).fetchall()
            return {"conversation": conversation, "workflows": workflows}

    def list(self, actor: ActorScope, *, limit: int = 50, before: datetime | None = None) -> list[dict[str, Any]]:
        with self.database.connection() as conn:
            if before:
                return conn.execute("SELECT * FROM conversations WHERE tenant_id=%s AND owner_user_id=%s AND brand_id=%s AND updated_at < %s ORDER BY updated_at DESC, conversation_id DESC LIMIT %s",
                                    (actor.tenant_id, actor.user_id, actor.brand_id, before, limit)).fetchall()
            return conn.execute("SELECT * FROM conversations WHERE tenant_id=%s AND owner_user_id=%s AND brand_id=%s ORDER BY updated_at DESC, conversation_id DESC LIMIT %s",
                                (actor.tenant_id, actor.user_id, actor.brand_id, limit)).fetchall()

    def messages(self, conversation_id: UUID, actor: ActorScope, *, after: int = 0, limit: int = 100) -> list[MessageRecord]:
        with self.database.connection() as conn:
            self._authorize_conversation(conn, conversation_id, actor)
            rows = conn.execute("SELECT * FROM conversation_messages WHERE conversation_id=%s AND seq>%s ORDER BY seq LIMIT %s", (conversation_id, after, limit)).fetchall()
            return [MessageRecord.model_validate(row) for row in rows]

    def append_message(self, conversation_id: UUID, actor: ActorScope, *, workflow_id: UUID | None,
                       role: str, content: str | None, content_json: dict[str, Any] | None = None,
                       source_event_id: UUID | None = None) -> MessageRecord:
        if role not in {"user", "assistant", "system", "tool"}:
            raise ValueError("unsupported message role")
        with self.database.transaction() as conn:
            conversation = self._authorize_conversation(conn, conversation_id, actor, lock=True)
            if workflow_id is not None:
                workflow = conn.execute("SELECT 1 FROM workflows WHERE workflow_id=%s AND conversation_id=%s", (workflow_id, conversation_id)).fetchone()
                if not workflow:
                    raise PermissionError("workflow is unavailable")
            if source_event_id is not None:
                event = conn.execute("SELECT conversation_id FROM conversation_events WHERE event_id=%s", (source_event_id,)).fetchone()
                if not event or event["conversation_id"] != conversation_id:
                    raise ValueError("message source event does not belong to conversation")
            seq = conversation["message_seq"] + 1
            message_id = uuid4()
            row = conn.execute("INSERT INTO conversation_messages(message_id,conversation_id,workflow_id,seq,role,content,content_json,source_event_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (message_id, conversation_id, workflow_id, seq, role, content, json.dumps(content_json) if content_json is not None else None, source_event_id)).fetchone()
            conn.execute("UPDATE conversations SET message_seq=%s,updated_at=now() WHERE conversation_id=%s", (seq, conversation_id))
            return MessageRecord.model_validate(row)

    def save_summary(self, conversation_id: UUID, actor: ActorScope, workflow_id: UUID,
                     covered_until_message_id: UUID, source_message_ids: list[UUID], summary: dict[str, Any]) -> ContextSummary:
        if covered_until_message_id not in source_message_ids:
            raise ValueError("summary watermark must be an included source message")
        with self.database.transaction() as conn:
            self._authorize_conversation(conn, conversation_id, actor)
            if conn.execute("SELECT 1 FROM workflows WHERE workflow_id=%s AND conversation_id=%s", (workflow_id, conversation_id)).fetchone() is None:
                raise PermissionError("workflow is unavailable")
            if len(source_message_ids) != len(set(source_message_ids)):
                raise ValueError("summary source message IDs must be unique")
            count = conn.execute("SELECT count(*) AS n FROM conversation_messages WHERE conversation_id=%s AND workflow_id=%s AND message_id=ANY(%s)",
                                 (conversation_id, workflow_id, source_message_ids)).fetchone()["n"]
            if count != len(source_message_ids):
                raise ValueError("summary sources must belong to the workflow")
            row = conn.execute("INSERT INTO context_summaries(conversation_id,workflow_id,covered_until_message_id,source_message_ids,summary_json) VALUES (%s,%s,%s,%s,%s) ON CONFLICT(workflow_id,covered_until_message_id) DO NOTHING RETURNING *",
                (conversation_id, workflow_id, covered_until_message_id, json.dumps([str(item) for item in source_message_ids]), json.dumps(summary))).fetchone()
            if row is None:
                row = conn.execute("SELECT * FROM context_summaries WHERE workflow_id=%s AND covered_until_message_id=%s", (workflow_id, covered_until_message_id)).fetchone()
                if row["source_message_ids"] != [str(item) for item in source_message_ids] or row["summary_json"] != summary:
                    raise EventConflict("summary watermark was reused with different content")
            return ContextSummary.model_validate(row)

    def operation_records(self, conversation_id: UUID, actor: ActorScope, workflow_id: UUID) -> list[dict[str, Any]]:
        with self.database.connection() as conn:
            self._authorize_conversation(conn, conversation_id, actor)
            return conn.execute("SELECT * FROM workflow_operations WHERE workflow_id=%s AND conversation_id=%s ORDER BY created_at,operation_id", (workflow_id, conversation_id)).fetchall()

    def events(self, conversation_id: UUID, actor: ActorScope, *, after: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        with self.database.connection() as conn:
            self._authorize_conversation(conn, conversation_id, actor)
            return conn.execute("SELECT * FROM conversation_events WHERE conversation_id=%s AND seq>%s ORDER BY seq LIMIT %s", (conversation_id, after, limit)).fetchall()

    def accepted_event(self, conversation_id: UUID, actor: ActorScope, event_id: UUID) -> tuple[dict[str, Any], WorkflowDraft, int]:
        with self.database.connection() as conn:
            self._authorize_conversation(conn, conversation_id, actor)
            row = conn.execute("SELECT e.*,w.draft_json,w.version FROM conversation_events e JOIN workflows w ON w.workflow_id=e.workflow_id WHERE e.event_id=%s AND e.conversation_id=%s AND w.tenant_id=%s AND w.owner_user_id=%s AND w.brand_id=%s",
                               (event_id, conversation_id, actor.tenant_id, actor.user_id, actor.brand_id)).fetchone()
            if row is None:
                raise PermissionError("event is unavailable")
            return row, WorkflowDraft.model_validate(row["draft_json"]), row["version"]

    def finish_graph_event(self, conversation_id: UUID, actor: ActorScope, event_id: UUID, draft: WorkflowDraft,
                           *, owner: str, token: int, expected_version: int, status: str = "applied",
                           error_code: str | None = None) -> EventReceipt:
        """Commit graph output, operation ledger, UI snapshot and event receipt atomically."""
        if status not in {"applied", "rejected", "unknown"}:
            raise ValueError("invalid graph event outcome")
        with self.database.transaction() as conn:
            conversation = self._authorize_conversation(conn, conversation_id, actor, lock=True)
            event = conn.execute("SELECT * FROM conversation_events WHERE event_id=%s AND conversation_id=%s FOR UPDATE", (event_id, conversation_id)).fetchone()
            if not event:
                raise ValueError("event not found")
            if draft.task_id != event["workflow_id"]:
                raise ValueError("graph draft does not belong to the event workflow")
            if event["status"] != "accepted":
                return EventReceipt.model_validate(event["receipt"])
            lease = conn.execute("SELECT fencing_token FROM workflow_leases WHERE workflow_id=%s AND lease_owner=%s AND fencing_token=%s AND lease_until>now() FOR UPDATE", (event["workflow_id"], owner, token)).fetchone()
            if not lease:
                raise RuntimeError("workflow lease lost")
            workflow = conn.execute("SELECT version FROM workflows WHERE workflow_id=%s AND conversation_id=%s FOR UPDATE", (event["workflow_id"], conversation_id)).fetchone()
            if not workflow or workflow["version"] != expected_version:
                raise RuntimeError("workflow version changed while processing event")
            conn.execute("UPDATE workflows SET draft_json=%s,form_revision=%s,selection_revision=%s,stage=%s,business_campaign_id=%s,business_task_id=%s,downstream_task_id=%s,candidate_snapshot_ref=%s,candidate_snapshot_hash=%s,pending_ui=%s,version=version+1,updated_at=now() WHERE workflow_id=%s",
                         (json.dumps(draft.model_dump(mode="json")), draft.form_revision,draft.selection.revision,draft.stage.value,
                          draft.business_campaign_id,draft.business_task_id,draft.downstream_task_id,draft.candidate_snapshot_ref,
                          draft.selection.candidate_snapshot_hash,json.dumps(draft.pending_ui.model_dump(mode="json")) if draft.pending_ui else None,draft.task_id))
            self._sync_operations(conn, conversation_id, actor, draft)
            self._sync_selection_revision(conn, draft)
            receipt = EventReceipt(event_id=event_id,conversation_id=conversation_id,workflow_id=event["workflow_id"],
                client_event_id=event["client_event_id"],seq=event["seq"],status=status,body_hash=event["body_hash"],
                event_type=event["event_type"],error_code=error_code)
            ui_seq = conversation["event_seq"] + 1
            ui_id = uuid4()
            if draft.selection.snapshot_ref:
                ui_type = "selection_snapshot"
                payload = {"workflow_id":str(draft.task_id),"selection_revision":draft.selection.revision,
                           "candidate_snapshot_ref":str(draft.selection.snapshot_ref),"candidate_snapshot_hash":draft.selection.candidate_snapshot_hash,
                           "selected_kol_ids":draft.selection.selected_kol_ids,"pending_ui":draft.pending_ui.model_dump(mode="json") if draft.pending_ui else None}
            else:
                ui_type = "form_snapshot"
                payload = {"workflow_id":str(draft.task_id),"form_revision":draft.form_revision,"stage":draft.stage.value,
                           "source":"server_validated","snapshot":{"campaign":draft.campaign.model_dump(mode="json"),
                           "recommendation_task":draft.recommendation_task.model_dump(mode="json")},
                           "field_sources":{key:value.model_dump(mode="json") for key,value in draft.field_sources.items()},
                           "validation_errors":[issue.model_dump(mode="json") for issue in draft.validation_errors],
                           "pending_ui":draft.pending_ui.model_dump(mode="json") if draft.pending_ui else None}
            conn.execute("UPDATE conversations SET event_seq=%s,updated_at=now() WHERE conversation_id=%s",(ui_seq,conversation_id))
            conn.execute("INSERT INTO ui_events(conversation_id,workflow_id,seq,event_id,source_event_id,event_type,payload) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                         (conversation_id,draft.task_id,ui_seq,ui_id,event_id,ui_type,json.dumps(payload)))
            conn.execute("UPDATE conversation_events SET status=%s,receipt=%s,error_code=%s,processed_at=now() WHERE event_id=%s",
                         (status,json.dumps(receipt.model_dump(mode="json")),error_code,event_id))
            conn.execute("INSERT INTO outbox_events(conversation_id,workflow_id,source_event_id,topic,aggregate_id,payload) VALUES (%s,%s,%s,'ui.event.ready',%s,%s)",
                         (conversation_id,draft.task_id,event_id,str(draft.task_id),json.dumps({"event_id":str(ui_id),"seq":ui_seq})))
            return receipt

    def ui_events(self, conversation_id: UUID, actor: ActorScope, *, after: int = 0, limit: int = 100) -> list[UIEvent]:
        with self.database.connection() as conn:
            self._authorize_conversation(conn, conversation_id, actor)
            rows = conn.execute("SELECT * FROM ui_events WHERE conversation_id=%s AND seq>%s ORDER BY seq LIMIT %s", (conversation_id, after, limit)).fetchall()
            return [UIEvent.model_validate(row) for row in rows]

    def apply_form_event(self, conversation_id: UUID, actor: ActorScope, event_id: UUID,
                         *, owner: str, token: int) -> EventReceipt:
        """Apply one persisted form event, its UI projection and outbox in one transaction."""
        from pydantic import TypeAdapter
        from app.schemas.events import FormPatchEvent
        from app.graphs.campaign.nodes import apply_event, validate_and_route

        with self.database.transaction() as conn:
            conversation = self._authorize_conversation(conn, conversation_id, actor, lock=True)
            event_row = conn.execute("SELECT * FROM conversation_events WHERE event_id=%s AND conversation_id=%s FOR UPDATE", (event_id, conversation_id)).fetchone()
            if not event_row:
                raise ValueError("event not found")
            if event_row["status"] != "accepted":
                return EventReceipt.model_validate(event_row["receipt"])
            lease = conn.execute("SELECT fencing_token FROM workflow_leases WHERE workflow_id=%s AND lease_owner=%s AND fencing_token=%s AND lease_until>now() FOR UPDATE", (event_row["workflow_id"], owner, token)).fetchone()
            if not lease:
                raise RuntimeError("workflow lease lost")
            workflow = conn.execute("SELECT * FROM workflows WHERE workflow_id=%s AND conversation_id=%s FOR UPDATE", (event_row["workflow_id"], conversation_id)).fetchone()
            draft = WorkflowDraft.model_validate(workflow["draft_json"])
            event = TypeAdapter(AgentEvent).validate_python(event_row["payload"])
            if not isinstance(event, FormPatchEvent):
                raise ValueError("only form patches are supported by this persisted handler")
            try:
                applied = apply_event({"draft": draft, "incoming_event": event})
                updated = validate_and_route({"draft": applied["draft"]})["draft"]
                status = "applied"
                error_code = None
            except ValueError:
                updated = draft
                status = "conflict"
                error_code = "invalid_or_stale_form_patch"
            receipt = EventReceipt(event_id=event_id, conversation_id=conversation_id, workflow_id=event_row["workflow_id"],
                                   client_event_id=event.client_event_id, seq=event_row["seq"], status=status,
                                   body_hash=event_row["body_hash"], event_type=event.type, error_code=error_code)
            if status == "applied":
                conn.execute("UPDATE workflows SET draft_json=%s, form_revision=%s, selection_revision=%s, stage=%s, pending_ui=%s, version=version+1, updated_at=now() WHERE workflow_id=%s",
                             (json.dumps(updated.model_dump(mode="json")), updated.form_revision, updated.selection.revision,
                              updated.stage.value, json.dumps(updated.pending_ui.model_dump(mode="json")) if updated.pending_ui else None, updated.task_id))
            ui_seq = conversation["event_seq"] + 1
            ui_id = uuid4()
            payload = {"form_revision": updated.form_revision, "source": "form_edit", "snapshot": {
                "campaign": updated.campaign.model_dump(mode="json"),
                "recommendation_task": updated.recommendation_task.model_dump(mode="json"),
            }, "field_sources": {key: value.model_dump(mode="json") for key, value in updated.field_sources.items()},
                "validation_errors": [issue.model_dump(mode="json") for issue in updated.validation_errors],
                "pending_ui": updated.pending_ui.model_dump(mode="json") if updated.pending_ui else None}
            ui_type = "form_snapshot" if status == "applied" else "event_rejected"
            if status != "applied":
                payload = {"error_code": error_code, "form_revision": updated.form_revision}
            conn.execute("UPDATE conversations SET event_seq=%s, updated_at=now() WHERE conversation_id=%s", (ui_seq, conversation_id))
            conn.execute("INSERT INTO ui_events(conversation_id,workflow_id,seq,event_id,source_event_id,event_type,payload) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                         (conversation_id, updated.task_id, ui_seq, ui_id, event_id, ui_type, json.dumps(payload)))
            conn.execute("UPDATE conversation_events SET status=%s, receipt=%s, error_code=%s, processed_at=now() WHERE event_id=%s", (status, json.dumps(receipt.model_dump(mode="json")), error_code, event_id))
            conn.execute("INSERT INTO outbox_events(conversation_id,workflow_id,source_event_id,topic,aggregate_id,payload) VALUES (%s,%s,%s,'ui.event.ready',%s,%s)",
                         (conversation_id, updated.task_id, event_id, str(updated.task_id), json.dumps({"event_id": str(ui_id), "seq": ui_seq})))
            return receipt

    def accept_event(self, conversation_id: UUID, actor: ActorScope, event: AgentEvent) -> EventReceipt:
        body_hash = canonical_event_hash(event)
        with self.database.transaction() as conn:
            conversation = self._authorize_conversation(conn, conversation_id, actor, lock=True)
            workflow_id = UUID(str(event.task_id))
            workflow = conn.execute("SELECT workflow_id FROM workflows WHERE workflow_id=%s AND conversation_id=%s AND tenant_id=%s AND owner_user_id=%s AND brand_id=%s FOR UPDATE",
                                    (workflow_id, conversation_id, actor.tenant_id, actor.user_id, actor.brand_id)).fetchone()
            if workflow is None:
                raise PermissionError("workflow is unavailable for the current actor and brand")
            prior = conn.execute("SELECT * FROM conversation_events WHERE conversation_id=%s AND client_event_id=%s", (conversation_id, event.client_event_id)).fetchone()
            if prior:
                if prior["body_hash"] != body_hash:
                    raise EventConflict("client event ID was reused with different content")
                return EventReceipt.model_validate(prior["receipt"])
            seq = conversation["event_seq"] + 1
            event_id = uuid4()
            receipt = EventReceipt(event_id=event_id, conversation_id=conversation_id, workflow_id=workflow_id,
                                   client_event_id=event.client_event_id, seq=seq, status="accepted", body_hash=body_hash,
                                   event_type=event.type)
            conn.execute("UPDATE conversations SET event_seq=%s, updated_at=now() WHERE conversation_id=%s", (seq, conversation_id))
            conn.execute("INSERT INTO conversation_events(event_id,conversation_id,workflow_id,seq,client_event_id,body_hash,event_type,payload,receipt) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                         (event_id, conversation_id, workflow_id, seq, event.client_event_id, body_hash, event.type,
                          json.dumps(event.model_dump(mode="json")), json.dumps(receipt.model_dump(mode="json"))))
            if isinstance(event, UserMessage):
                message_seq = conversation["message_seq"] + 1
                conn.execute("UPDATE conversations SET message_seq=%s WHERE conversation_id=%s", (message_seq, conversation_id))
                conn.execute("INSERT INTO conversation_messages(message_id,conversation_id,workflow_id,seq,role,content,source_event_id) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                             (uuid4(), conversation_id, workflow_id, message_seq, "user", event.text, event_id))
            conn.execute("INSERT INTO outbox_events(conversation_id,workflow_id,source_event_id,topic,aggregate_id,payload) VALUES (%s,%s,%s,%s,%s,%s)",
                         (conversation_id, workflow_id, event_id, "workflow.event.accepted", str(workflow_id), json.dumps(receipt.model_dump(mode="json"))))
            return receipt

    def save_workflow(self, conversation_id: UUID, actor: ActorScope, draft: WorkflowDraft, *, owner: str, fence: int, expected_version: int) -> None:
        with self.database.transaction() as conn:
            self._authorize_conversation(conn, conversation_id, actor, lock=True)
            lease = conn.execute("SELECT fencing_token FROM workflow_leases WHERE workflow_id=%s AND lease_owner=%s AND fencing_token=%s AND lease_until>now() FOR UPDATE", (draft.task_id, owner, fence)).fetchone()
            if not lease:
                raise RuntimeError("workflow lease lost")
            result = conn.execute("UPDATE workflows SET draft_json=%s, form_revision=%s, selection_revision=%s, stage=%s, business_campaign_id=%s, business_task_id=%s, downstream_task_id=%s, candidate_snapshot_ref=%s, candidate_snapshot_hash=%s, pending_ui=%s, version=version+1, updated_at=now() WHERE workflow_id=%s AND conversation_id=%s AND version=%s",
                                  (json.dumps(draft.model_dump(mode="json")), draft.form_revision, draft.selection.revision, draft.stage.value,
                                   draft.business_campaign_id, draft.business_task_id, draft.downstream_task_id, draft.candidate_snapshot_ref,
                                   draft.selection.candidate_snapshot_hash, json.dumps(draft.pending_ui.model_dump(mode="json")) if draft.pending_ui else None,
                                   draft.task_id, conversation_id, expected_version)).rowcount
            if result != 1:
                raise RuntimeError("workflow fencing/version check rejected state update")
            self._sync_operations(conn, conversation_id, actor, draft)
            self._sync_selection_revision(conn, draft)

    @staticmethod
    def _sync_operations(conn: Connection, conversation_id: UUID, actor: ActorScope, draft: WorkflowDraft) -> None:
        operations = []
        business = draft.pending_business_operation
        if business is not None:
            if (business.actor_tenant_id, business.actor_user_id, business.actor_brand_id, business.agent_task_id) != (actor.tenant_id, actor.user_id, actor.brand_id, draft.task_id):
                raise PermissionError("business operation actor/workflow mismatch")
            operations.append({
                "operation_id": business.operation_id, "type": business.operation_type.value,
                "request_id": business.request_id, "confirmation_id": business.confirmation_id,
                "form_revision": business.form_revision, "selection_revision": None,
                "payload_hash": business.payload_hash, "status": business.status.value,
                "campaign_id": business.campaign_id or draft.business_campaign_id,
                "business_task_id": business.business_task_id or draft.business_task_id,
                "downstream_task_id": None, "receipt": None,
            })
        run = draft.recommendation_run
        if run is not None:
            if (run.actor_tenant_id, run.actor_user_id, run.actor_brand_id, run.agent_task_id) != (actor.tenant_id, actor.user_id, actor.brand_id, draft.task_id):
                raise PermissionError("recommendation actor/workflow mismatch")
            if not business or run.operation_id != business.operation_id:
                operations.append({
                "operation_id": run.operation_id, "type": "start_recommendation", "request_id": run.request_id,
                "confirmation_id": run.confirmation_id, "form_revision": run.form_revision,
                "selection_revision": None, "payload_hash": run.payload_hash, "status": run.status.value,
                "campaign_id": run.campaign_id, "business_task_id": run.business_task_id,
                "downstream_task_id": run.downstream_task_id, "receipt": None,
                })
        for name, operation in (("save_selection", draft.selection.save_operation),
                                ("confirm_selection", draft.selection.final_operation)):
            if operation is not None:
                preview = operation.preview
                if (preview.actor_tenant_id, preview.actor_user_id, preview.actor_brand_id, preview.agent_task_id) != (actor.tenant_id, actor.user_id, actor.brand_id, draft.task_id):
                    raise PermissionError("selection actor/workflow mismatch")
                operations.append({
                    "operation_id": preview.operation_id, "type": name, "request_id": preview.request_id,
                    "confirmation_id": preview.confirmation_id, "form_revision": preview.form_revision,
                    "selection_revision": preview.selection_revision, "payload_hash": preview.payload_hash,
                    "status": operation.status.value, "campaign_id": preview.campaign_id,
                    "business_task_id": preview.business_task_id, "downstream_task_id": preview.downstream_task_id,
                    "receipt": operation.receipt.model_dump(mode="json") if operation.receipt else None,
                })
        for item in operations:
            result = conn.execute("INSERT INTO workflow_operations(operation_id,conversation_id,workflow_id,tenant_id,owner_user_id,brand_id,operation_type,request_id,confirmation_id,form_revision,selection_revision,payload_hash,status,campaign_id,business_task_id,downstream_task_id,receipt) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(operation_id) DO UPDATE SET status=EXCLUDED.status, downstream_task_id=EXCLUDED.downstream_task_id, receipt=EXCLUDED.receipt, updated_at=now() WHERE workflow_operations.workflow_id=EXCLUDED.workflow_id AND workflow_operations.conversation_id=EXCLUDED.conversation_id AND workflow_operations.request_id=EXCLUDED.request_id AND workflow_operations.confirmation_id IS NOT DISTINCT FROM EXCLUDED.confirmation_id AND workflow_operations.payload_hash=EXCLUDED.payload_hash AND workflow_operations.operation_type=EXCLUDED.operation_type AND workflow_operations.form_revision=EXCLUDED.form_revision AND workflow_operations.selection_revision IS NOT DISTINCT FROM EXCLUDED.selection_revision",
                (item["operation_id"], conversation_id, draft.task_id, actor.tenant_id, actor.user_id, actor.brand_id,
                 item["type"], item["request_id"], item["confirmation_id"], item["form_revision"],
                 item["selection_revision"], item["payload_hash"], item["status"], item["campaign_id"],
                 item["business_task_id"], item["downstream_task_id"], json.dumps(item["receipt"]) if item["receipt"] else None))
            if result.rowcount != 1:
                raise EventConflict("operation identity or payload changed")

    @staticmethod
    def _sync_selection_revision(conn: Connection, draft: WorkflowDraft) -> None:
        selection = draft.selection
        if selection.snapshot_ref is None and selection.candidate_snapshot_hash is None and selection.revision == 0:
            return
        conn.execute("INSERT INTO selection_revisions(workflow_id,revision,snapshot_ref,snapshot_hash,selected_kol_ids,rejected_kols,cancelled_kol_ids) VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(workflow_id,revision) DO NOTHING",
            (draft.task_id, selection.revision, str(selection.snapshot_ref) if selection.snapshot_ref else None,
             selection.candidate_snapshot_hash, json.dumps(selection.selected_kol_ids), json.dumps(selection.rejected_kols), json.dumps(selection.cancelled_kol_ids)))
        old = conn.execute("SELECT * FROM selection_revisions WHERE workflow_id=%s AND revision=%s", (draft.task_id, selection.revision)).fetchone()
        if (old["snapshot_ref"], old["snapshot_hash"], old["selected_kol_ids"], old["rejected_kols"], old["cancelled_kol_ids"]) != (
            str(selection.snapshot_ref) if selection.snapshot_ref else None, selection.candidate_snapshot_hash,
            selection.selected_kol_ids, selection.rejected_kols, selection.cancelled_kol_ids,
        ):
            raise EventConflict("selection revision was reused with different content")

    def claim_lease(self, workflow_id: UUID, owner: str, ttl_seconds: int = 30) -> int | None:
        with self.database.transaction() as conn:
            if conn.execute("SELECT workflow_id FROM workflows WHERE workflow_id=%s FOR UPDATE", (workflow_id,)).fetchone() is None:
                raise ValueError("workflow does not exist")
            row = conn.execute("SELECT *, lease_until > now() AS active FROM workflow_leases WHERE workflow_id=%s FOR UPDATE", (workflow_id,)).fetchone()
            if row and row["active"]:
                return None
            if row:
                token = row["fencing_token"] + 1
                conn.execute("UPDATE workflow_leases SET lease_owner=%s, lease_until=now()+(%s * interval '1 second'), fencing_token=%s, updated_at=now() WHERE workflow_id=%s", (owner, ttl_seconds, token, workflow_id))
            else:
                token = 1
                conn.execute("INSERT INTO workflow_leases(workflow_id,lease_owner,lease_until,fencing_token) VALUES (%s,%s,now()+(%s * interval '1 second'),%s)", (workflow_id, owner, ttl_seconds, token))
            return token

    def renew_lease(self, workflow_id: UUID, owner: str, token: int, ttl_seconds: int = 30) -> bool:
        with self.database.transaction() as conn:
            return conn.execute("UPDATE workflow_leases SET lease_until=now()+(%s * interval '1 second') WHERE workflow_id=%s AND lease_owner=%s AND fencing_token=%s AND lease_until>now()", (ttl_seconds, workflow_id, owner, token)).rowcount == 1

    def release_lease(self, workflow_id: UUID, owner: str, token: int) -> bool:
        with self.database.transaction() as conn:
            return conn.execute("UPDATE workflow_leases SET lease_until=now() WHERE workflow_id=%s AND lease_owner=%s AND fencing_token=%s AND lease_until>now()", (workflow_id, owner, token)).rowcount == 1

    def event_context(self, event_id: UUID) -> tuple[UUID, ActorScope, str]:
        """Read routing metadata; the worker must reauthorize this actor before processing."""
        with self.database.connection() as conn:
            row = conn.execute("SELECT e.conversation_id, e.event_type, w.tenant_id, w.owner_user_id, w.brand_id FROM conversation_events e JOIN workflows w ON w.workflow_id=e.workflow_id WHERE e.event_id=%s", (event_id,)).fetchone()
            if not row:
                raise ValueError("event not found")
            return row["conversation_id"], ActorScope(tenant_id=row["tenant_id"], user_id=row["owner_user_id"], brand_id=row["brand_id"]), row["event_type"]

    def claim_outbox(self, owner: str, *, topic: str, limit: int = 10, ttl_seconds: int = 30) -> list[OutboxLease]:
        with self.database.transaction() as conn:
            rows = conn.execute("""SELECT o.* FROM outbox_events o
                JOIN conversation_events e ON e.event_id=o.source_event_id
                WHERE o.topic=%s AND (o.status IN ('pending','retry') OR (o.status='leased' AND o.lease_until<now()))
                  AND o.available_at<=now()
                  AND NOT EXISTS (
                    SELECT 1 FROM conversation_events earlier
                    WHERE earlier.workflow_id=e.workflow_id AND earlier.seq<e.seq
                      AND earlier.status IN ('accepted','processing')
                  )
                ORDER BY e.seq FOR UPDATE OF o SKIP LOCKED LIMIT %s""", (topic, limit)).fetchall()
            result = []
            for row in rows:
                conn.execute("UPDATE outbox_events SET status='leased', lease_owner=%s, lease_until=now()+(%s * interval '1 second'), attempts=attempts+1, fencing_token=fencing_token+1 WHERE outbox_id=%s", (owner, ttl_seconds, row["outbox_id"]))
                result.append(OutboxLease(outbox_id=row["outbox_id"], workflow_id=row["workflow_id"], source_event_id=row["source_event_id"], topic=row["topic"], aggregate_id=row["aggregate_id"], payload=row["payload"], lease_owner=owner, fencing_token=row["fencing_token"] + 1, attempts=row["attempts"] + 1))
            return result

    def finish_outbox(self, lease: OutboxLease, *, success: bool, error: str | None = None) -> bool:
        with self.database.transaction() as conn:
            status = "delivered" if success else "retry"
            return conn.execute("UPDATE outbox_events SET status=%s, delivered_at=CASE WHEN %s THEN now() ELSE delivered_at END, lease_owner=NULL, lease_until=NULL, last_error=%s, available_at=CASE WHEN %s THEN available_at ELSE now()+interval '5 seconds' END WHERE outbox_id=%s AND status='leased' AND lease_owner=%s AND fencing_token=%s AND lease_until>now()", (status, success, error, success, lease.outbox_id, lease.lease_owner, lease.fencing_token)).rowcount == 1
