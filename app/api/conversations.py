"""Optional durable API; fails closed until trusted providers are installed by deployment."""

import asyncio
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.persistence.repositories import EventConflict
from app.schemas.business_api import ActorScope
from app.schemas.events import FormPatchEvent
from app.services.sse import replay_cursor, stream_ui_events

router = APIRouter(prefix="/api/agent/conversations", tags=["conversations"])


class NewConversation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, max_length=200)


async def authorized_actor(request: Request, authorization: Annotated[str | None, Header()] = None) -> ActorScope:
    authenticator = getattr(request.app.state, "actor_authenticator", None)
    authorizer = getattr(request.app.state, "brand_authorizer", None)
    if authenticator is None or authorizer is None or getattr(request.app.state, "conversation_repository", None) is None:
        raise HTTPException(status_code=503, detail="Identity or persistence service is unavailable")
    if not authorization or not authorization.startswith("Bearer ") or not authorization[7:].strip():
        raise HTTPException(status_code=401, detail="Authentication required")
    try:
        actor = await authenticator.authenticate(authorization[7:])
        if not isinstance(actor, ActorScope) or not await asyncio.to_thread(authorizer.authorize, actor):
            raise PermissionError
    except Exception:
        raise HTTPException(status_code=403, detail="Access unavailable") from None
    return actor


Actor = Annotated[ActorScope, Depends(authorized_actor)]


@router.post("", status_code=201)
async def create(request: Request, body: NewConversation, actor: Actor):
    ref = await asyncio.to_thread(request.app.state.conversation_repository.create, actor, title=body.title)
    return {"conversation_id": ref.conversation_id, "workflow_id": ref.workflow_id}


@router.get("")
async def list_conversations(request: Request, actor: Actor, limit: int = Query(default=50, ge=1, le=100)):
    rows = await asyncio.to_thread(request.app.state.conversation_repository.list, actor, limit=limit)
    return [{key: row[key] for key in ("conversation_id", "title", "status", "active_workflow_id", "created_at", "updated_at")} for row in rows]


@router.get("/{conversation_id}")
async def detail(conversation_id: UUID, request: Request, actor: Actor):
    try:
        data = await asyncio.to_thread(request.app.state.conversation_repository.get, conversation_id, actor)
        conversation = data["conversation"]
        return {
            "conversation": {key: conversation[key] for key in ("conversation_id", "title", "status", "active_workflow_id", "event_seq", "message_seq", "created_at", "updated_at")},
            "workflows": [{key: row[key] for key in ("workflow_id", "form_revision", "selection_revision", "stage", "pending_ui", "version")} for row in data["workflows"]],
        }
    except PermissionError:
        raise HTTPException(status_code=404, detail="Conversation unavailable") from None


@router.get("/{conversation_id}/messages")
async def messages(conversation_id: UUID, request: Request, actor: Actor,
                   after: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=100)):
    try:
        return await asyncio.to_thread(request.app.state.conversation_repository.messages, conversation_id, actor, after=after, limit=limit)
    except PermissionError:
        raise HTTPException(status_code=404, detail="Conversation unavailable") from None


@router.post("/{conversation_id}/events", status_code=202)
async def submit(conversation_id: UUID, request: Request, event: FormPatchEvent, actor: Actor):
    try:
        return await asyncio.to_thread(request.app.state.conversation_repository.accept_event, conversation_id, actor, event)
    except PermissionError:
        raise HTTPException(status_code=404, detail="Conversation unavailable") from None
    except EventConflict:
        raise HTTPException(status_code=409, detail="Event ID conflicts with an existing event") from None


@router.get("/{conversation_id}/stream")
async def stream(conversation_id: UUID, request: Request, actor: Actor,
                 after: int | None = Query(default=None, ge=0),
                 last_event_id: Annotated[str | None, Header()] = None):
    try:
        cursor = replay_cursor(last_event_id, after)
        await asyncio.to_thread(request.app.state.conversation_repository.get, conversation_id, actor)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid event cursor") from None
    except PermissionError:
        raise HTTPException(status_code=404, detail="Conversation unavailable") from None
    events = stream_ui_events(request.app.state.conversation_repository, request.app.state.brand_authorizer,
                              conversation_id, actor, after=cursor)
    return StreamingResponse(events, media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
