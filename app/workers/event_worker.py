"""Explicitly limited event consumer; external business writes are never dispatched."""

from app.persistence.repositories import ConversationRepository
from app.services.identity import CurrentBrandAuthorizer


class EventWorker:
    def __init__(self, repository: ConversationRepository, authorizer: CurrentBrandAuthorizer, owner: str):
        self.repository = repository
        self.authorizer = authorizer
        self.owner = owner

    def run_once(self) -> int:
        completed = 0
        leases = self.repository.claim_outbox(self.owner, topic="workflow.event.accepted")
        for item in leases:
            if item.source_event_id is None:
                self.repository.finish_outbox(item, success=False, error="missing_event")
                continue
            workflow_token = None
            try:
                conversation_id, actor, event_type = self.repository.event_context(item.source_event_id)
                if not self.authorizer.authorize(actor):
                    self.repository.finish_outbox(item, success=False, error="authorization_unavailable")
                    continue
                if event_type != "form_patch":
                    self.repository.finish_outbox(item, success=False, error="unsupported_event_type")
                    continue
                workflow_token = self.repository.claim_lease(item.workflow_id, self.owner)
                if workflow_token is None:
                    self.repository.finish_outbox(item, success=False, error="workflow_busy")
                    continue
                self.repository.apply_form_event(conversation_id, actor, item.source_event_id,
                                                 owner=self.owner, token=workflow_token)
                if self.repository.finish_outbox(item, success=True):
                    completed += 1
            except Exception:
                # Do not include provider errors or event content in the outbox.
                self.repository.finish_outbox(item, success=False, error="processing_failed")
            finally:
                if workflow_token is not None:
                    self.repository.release_lease(item.workflow_id, self.owner, workflow_token)
        return completed
