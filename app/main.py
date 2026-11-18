"""ASGI application entry point."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import router
from app.config import get_settings
from app.services.workflow import WorkflowService


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.workflow = WorkflowService()
    yield
    # TODO(lifecycle, design §§3, 9): close database/model clients and workers;
    # initialize the production saver during deployment, not per request.


def create_app() -> FastAPI:
    app = FastAPI(title=get_settings().app_name, lifespan=lifespan)
    app.include_router(router)
    return app


app = create_app()
