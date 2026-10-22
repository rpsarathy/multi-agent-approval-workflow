"""Public FastAPI application: workflow API and approval UI.

The internal domain API is a separate ASGI app (app/internal_main.py) and is NOT mounted here,
so end users cannot reach the agents' tool endpoints.
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.api import workflows
from app.config import get_settings, validate_settings
from app.orchestrator import engine
from app.seed import seed

STATIC = Path(__file__).resolve().parent.parent / "static"
log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    validate_settings()     # refuse to start with weak or inconsistent secrets
    seed()                  # idempotent: creates tables and inserts synthetic data on first start
    recovered = engine.recover_interrupted_runs()
    if recovered:
        log.warning("marked %d interrupted run(s) FAILED for resume: %s", len(recovered), recovered)
    yield


app = FastAPI(title="Prior-Authorization Multi-Agent Orchestrator (synthetic demo)", version="1.1.0", lifespan=lifespan)
app.include_router(workflows.router)


@app.get("/", include_in_schema=False)
def ui():
    return FileResponse(STATIC / "index.html")


@app.get("/healthz")
def healthz():
    cfg = get_settings()
    return {"status": "ok", "llm_mode": cfg.effective_llm_mode, "model": cfg.llm_model if cfg.effective_llm_mode == "live" else "mock"}
