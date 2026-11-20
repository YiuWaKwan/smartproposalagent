"""Database-backed UI event replay; callers must provide current authorization."""

import asyncio
import json
from collections.abc import AsyncIterator
from uuid import UUID

from app.persistence.repositories import ConversationRepository
from app.schemas.business_api import ActorScope
from app.services.identity import CurrentBrandAuthorizer


def replay_cursor(last_event_id: str | None, after: int | None) -> int:
    try:
        previous = int(last_event_id) if last_event_id is not None else None
    except ValueError as exc:
        raise ValueError("invalid Last-Event-ID") from exc
    if (previous is not None and previous < 0) or (after is not None and after < 0):
        raise ValueError("cursor must be nonnegative")
    if previous is not None and after is not None and previous != after:
        raise ValueError("conflicting replay cursors")
    return previous if previous is not None else after or 0


async def stream_ui_events(
    repository: ConversationRepository,
    authorizer: CurrentBrandAuthorizer,
    conversation_id: UUID,
    actor: ActorScope,
    *,
    after: int = 0,
    heartbeat_seconds: float = 15,
) -> AsyncIterator[str]:
    """Poll committed UI events; no incoming event body or secret is sent."""
    cursor = after
    while True:
        if not await asyncio.to_thread(authorizer.authorize, actor):
            return
        try:
            events = await asyncio.to_thread(repository.ui_events, conversation_id, actor, after=cursor)
        except PermissionError:
            return
        if events:
            for event in events:
                data = json.dumps(event.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
                yield f"id: {event.seq}\nevent: {event.event_type}\ndata: {data}\n\n"
                cursor = event.seq
        else:
            yield ": heartbeat\n\n"
            await asyncio.sleep(heartbeat_seconds)
