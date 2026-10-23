"""FastAPI app: web UI and /healthz."""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config, db
from .scheduler import Scheduler

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config.load()
    conn = db.connect(cfg.db_path)
    db.migrate(conn)
    app.state.cfg = cfg
    app.state.db = conn
    app.state.scheduler = None
    if os.environ.get("SCHEDULER", "on") != "off":
        app.state.scheduler = Scheduler(cfg)
        app.state.scheduler.start()
    yield
    if app.state.scheduler:
        app.state.scheduler.shutdown()
    conn.close()


app = FastAPI(title="Deal Watch", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


@app.get("/healthz")
def healthz(request: Request):
    try:
        request.app.state.db.execute("SELECT 1").fetchone()
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=503)
    return {"status": "ok"}


@app.get("/")
def dashboard(request: Request):
    watches = request.app.state.db.execute(
        "SELECT id, name, enabled, typical_override FROM watches ORDER BY name"
    ).fetchall()
    return templates.TemplateResponse(request, "dashboard.html", {"watches": watches})
