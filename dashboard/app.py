"""Harness dashboard: health, trends, flows and raw data for the 2060 stack.

Run: uvicorn app:app --host 0.0.0.0 --port 8080   (DASHBOARD_DEMO=1 for synthetic data)
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
import secrets
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx
import psycopg
from fastapi import Body, FastAPI, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from backend import RANGES, Backend, err_text

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("dashboard")
logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per upstream call is noise

PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
DEMO = os.environ.get("DASHBOARD_DEMO") == "1"

if DEMO:
    from demo import DemoBackend
    backend: Backend = DemoBackend()
else:
    backend = Backend()


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not PASSWORD and not DEMO:
        await backend.close()
        raise RuntimeError("DASHBOARD_PASSWORD is required in live mode")
    task = asyncio.create_task(backend.poll_forever())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await backend.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if not PASSWORD and not DEMO and request.url.path != "/healthz":
        return JSONResponse({"error": "dashboard authentication unavailable"}, status_code=503)
    if PASSWORD and request.url.path != "/healthz":
        ok = False
        h = request.headers.get("authorization", "")
        if h.startswith("Basic "):
            try:
                _, _, pw = base64.b64decode(h[6:]).decode().partition(":")
                ok = secrets.compare_digest(pw.encode(), PASSWORD.encode())
            except (binascii.Error, UnicodeDecodeError):
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="harness dashboard"'})
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
    return resp


def _err(status: int):
    async def handler(_: Request, e: Exception):
        message = "Invalid request or upstream data" if status == 400 else err_text(e)
        return JSONResponse({"error": message}, status_code=status)
    return handler


# Expected upstream failures become JSON errors the UI shows inline (no traceback spam).
app.add_exception_handler(ValueError, _err(400))
for exc in (httpx.HTTPError, psycopg.Error, OSError, RuntimeError, KeyError, asyncio.TimeoutError):
    app.add_exception_handler(exc, _err(502))


Range = Query("7d", pattern="^(" + "|".join(RANGES) + ")$")


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/api/meta")
async def meta():
    return backend.meta()


@app.get("/api/health")
async def health():
    return await backend.health()


@app.get("/api/trends")
async def trends(range: str = Range):  # noqa: A002 - query param name
    return await backend.trends(range)


@app.get("/api/flows")
async def flows(range: str = Range):  # noqa: A002
    return await backend.flows(range)


@app.get("/api/findings")
async def findings(range: str = Range):  # noqa: A002
    return await backend.findings(range)


@app.get("/api/eval")
async def eval_scores(range: str = Range):  # noqa: A002
    return await backend.eval_scores(range)


@app.get("/api/run/{run_id}")
async def run_detail(run_id: str = PathParam(..., max_length=200)):
    return await backend.run_detail(run_id)


@app.post("/api/probe-routes")
async def probe_routes():
    return await backend.probe_routes()


@app.get("/api/raw/generations")
async def raw_generations(range: str = Range, cursor: str | None = Query(None, max_length=4096)):  # noqa: A002
    return await backend.raw_generations(range, cursor)


@app.get("/api/raw/traces")
async def raw_traces(range: str = Range, cursor: str | None = Query(None, max_length=4096)):  # noqa: A002
    return await backend.raw_traces(range, cursor)


@app.get("/api/raw/tables")
async def raw_tables():
    return await backend.raw_tables()


@app.get("/api/raw/table/{name}")
async def raw_table(name: str, page: int = Query(1, ge=1, le=100000)):
    return await backend.raw_table(name, page)


@app.get("/api/raw/qdrant")
async def raw_qdrant_list():
    return await backend.raw_qdrant(None)


@app.get("/api/raw/qdrant/{name}")
async def raw_qdrant(name: str, offset: str | None = None):
    off: int | str | None = int(offset) if offset and offset.isdigit() else offset
    return await backend.raw_qdrant(name, off)


@app.post("/api/raw/sql")
async def raw_sql(body: dict = Body(...)):
    return await backend.raw_sql(str(body.get("sql", ""))[:20000])


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
