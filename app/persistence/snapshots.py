"""Bounded PostgreSQL snapshot store scoped to one authorized workflow."""

from uuid import UUID

from app.persistence.database import Database
from app.schemas.business_api import ActorScope
from app.schemas.recommendation import JavaRecommendationResults, RecommendationSnapshotReference
from app.services.recommendations import recommendation_content_hash


class PostgresRecommendationSnapshotStore:
    def __init__(self, database: Database, workflow_id: UUID, actor: ActorScope, max_bytes: int = 5_000_000):
        self.database = database
        self.workflow_id = workflow_id
        self.actor = actor
        self.max_bytes = max_bytes

    def put(self, reference: RecommendationSnapshotReference, results: JavaRecommendationResults) -> None:
        body = results.model_dump_json(by_alias=True, exclude_none=True)
        if len(body.encode()) > self.max_bytes or recommendation_content_hash(results) != reference.content_hash:
            raise ValueError("snapshot exceeds size limit or hash does not match")
        if reference.java_persistence_verified:
            raise ValueError("local storage cannot attest Java snapshot persistence")
        with self.database.transaction() as conn:
            workflow = conn.execute("SELECT business_campaign_id,business_task_id,downstream_task_id FROM workflows WHERE workflow_id=%s AND tenant_id=%s AND owner_user_id=%s AND brand_id=%s FOR UPDATE",
                (self.workflow_id, self.actor.tenant_id, self.actor.user_id, self.actor.brand_id)).fetchone()
            if not workflow or (workflow["business_campaign_id"], workflow["business_task_id"], workflow["downstream_task_id"]) != (reference.campaign_id, reference.business_task_id, reference.downstream_task_id):
                raise PermissionError("snapshot does not match the authorized workflow")
            conn.execute("INSERT INTO result_snapshots(snapshot_ref,workflow_id,tenant_id,owner_user_id,brand_id,content_hash,metadata,results) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (str(reference.snapshot_ref), self.workflow_id, self.actor.tenant_id, self.actor.user_id, self.actor.brand_id,
                 reference.content_hash, reference.model_dump_json(), body))

    def _read(self, snapshot_ref: UUID):
        with self.database.connection() as conn:
            return conn.execute("SELECT metadata,results FROM result_snapshots WHERE snapshot_ref=%s AND workflow_id=%s AND tenant_id=%s AND owner_user_id=%s AND brand_id=%s", (str(snapshot_ref), self.workflow_id, self.actor.tenant_id, self.actor.user_id, self.actor.brand_id)).fetchone()

    def get_reference(self, snapshot_ref: UUID) -> RecommendationSnapshotReference | None:
        row = self._read(snapshot_ref)
        return RecommendationSnapshotReference.model_validate(row["metadata"]) if row else None

    def get_results(self, snapshot_ref: UUID) -> JavaRecommendationResults | None:
        row = self._read(snapshot_ref)
        return JavaRecommendationResults.model_validate(row["results"]) if row else None
