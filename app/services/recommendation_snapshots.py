"""Bounded local snapshot index; references Java-owned results and is process-local."""

from threading import RLock
from uuid import UUID

from app.schemas.recommendation import JavaRecommendationResults, RecommendationSnapshotReference


class VolatileRecommendationSnapshotStore:
    """Testing/local implementation only; never advertises durable persistence."""

    def __init__(self, max_entries: int = 256):
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self._max_entries = max_entries
        self._lock = RLock()
        self._refs: dict[UUID, RecommendationSnapshotReference] = {}
        self._results: dict[UUID, JavaRecommendationResults] = {}

    def put(self, reference: RecommendationSnapshotReference, results: JavaRecommendationResults) -> None:
        if reference.java_persistence_verified:
            raise ValueError("volatile store cannot claim Java-owned durable persistence")
        if len(self._refs) >= self._max_entries and reference.snapshot_ref not in self._refs:
            raise RuntimeError("volatile recommendation snapshot capacity reached")
        with self._lock:
            if len(self._refs) >= self._max_entries and reference.snapshot_ref not in self._refs:
                raise RuntimeError("volatile recommendation snapshot capacity reached")
            self._refs[reference.snapshot_ref] = reference.model_copy(deep=True)
            self._results[reference.snapshot_ref] = results.model_copy(deep=True)

    def get_reference(self, snapshot_ref: UUID) -> RecommendationSnapshotReference | None:
        with self._lock:
            value = self._refs.get(snapshot_ref)
            return value.model_copy(deep=True) if value else None

    def get_results(self, snapshot_ref: UUID) -> JavaRecommendationResults | None:
        with self._lock:
            value = self._results.get(snapshot_ref)
            return value.model_copy(deep=True) if value else None
