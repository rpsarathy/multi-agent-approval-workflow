"""Internal application API as a separate ASGI app.

In-process mode (default) the agents call it over ASGI without a socket. To run it as
its own service: `uvicorn app.internal_main:internal_app --port 8001` and set
INTERNAL_API_BASE_URL=http://127.0.0.1:8001 plus a shared INTERNAL_API_SECRET in both processes.
Bind it to localhost / a private network only; it is not meant to be reachable by end users.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import internal
from app.config import validate_settings
from app.seed import seed


@asynccontextmanager
async def lifespan(_app: FastAPI):
    validate_settings()
    seed()
    yield


internal_app = FastAPI(title="Internal domain API (agents only)", version="1.0.0", lifespan=lifespan,
                       docs_url=None, redoc_url=None, openapi_url=None)
internal_app.include_router(internal.router)
