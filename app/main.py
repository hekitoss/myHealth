"""Веб-сервер: API для интерфейса, OAuth Polar, чат с AI."""
import secrets
from datetime import datetime, timedelta

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import ai, db, demo, importer, metrics, polar
from .config import STATIC_DIR

db.init()
app = FastAPI(title="MyHealth")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_oauth_states: set[str] = set()


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/status")
def status():
    return {
        "polar_configured": polar.is_configured(),
        "polar_connected": polar.is_connected(),
        "ai_configured": ai.is_configured(),
        "workouts": db.count_workouts(),
        "hr_max": db.hr_max(),
        "last_sync": db.get_setting("last_sync"),
    }


@app.get("/api/workouts")
def workouts(days: int | None = None):
    since = (datetime.now() - timedelta(days=days)).isoformat() if days else None
    return db.list_workouts(since)


@app.get("/api/workouts/{wid}")
def workout(wid: int):
    w = db.get_workout(wid)
    if not w:
        raise HTTPException(404, "Тренировка не найдена")
    return w


@app.get("/api/weekly")
def weekly(weeks: int = 12):
    return metrics.weekly_summary(db.list_workouts(), weeks)


class Settings(BaseModel):
    hr_max: int = Field(ge=120, le=230)


@app.put("/api/settings")
def update_settings(s: Settings):
    db.set_setting("hr_max", s.hr_max)
    db.recompute_zones()
    return {"ok": True}


# --- Polar -------------------------------------------------------------------

@app.get("/polar/connect")
def polar_connect():
    if not polar.is_configured():
        raise HTTPException(400, "Заполните POLAR_CLIENT_ID и POLAR_CLIENT_SECRET в .env")
    state = secrets.token_urlsafe(16)
    _oauth_states.add(state)
    return RedirectResponse(polar.authorization_url(state))


@app.get("/oauth2_callback")
def polar_callback(code: str | None = None, state: str | None = None, error: str | None = None):
    if error or not code:
        return RedirectResponse(f"/?polar=error&msg={error or 'no_code'}")
    if state not in _oauth_states:
        return RedirectResponse("/?polar=error&msg=bad_state")
    _oauth_states.discard(state)
    try:
        polar.connect(code)
    except polar.PolarError:
        return RedirectResponse("/?polar=error&msg=connect_failed")
    return RedirectResponse("/?polar=connected")


@app.post("/api/polar/disconnect")
def polar_disconnect():
    polar.disconnect()
    return {"ok": True}


@app.post("/api/sync")
def sync():
    try:
        result = polar.sync()
    except polar.PolarError as e:
        raise HTTPException(400, str(e))
    db.set_setting("last_sync", datetime.now().isoformat(timespec="seconds"))
    return result


# --- импорт и демо -----------------------------------------------------------

@app.post("/api/import")
async def import_files(files: list[UploadFile] = File(...)):
    new = updated = 0
    errors = []
    for f in files:
        try:
            _, is_new = importer.import_file(f.filename or "", await f.read())
            new += is_new
            updated += not is_new
        except (importer.ImportErrorBadFile, KeyError, ValueError) as e:
            errors.append(f"{f.filename}: {e}")
    return {"new": new, "updated": updated, "errors": errors}


@app.post("/api/demo")
def demo_create():
    return {"created": demo.generate()}


@app.delete("/api/demo")
def demo_delete():
    return {"deleted": db.delete_source("demo")}


# --- AI ----------------------------------------------------------------------

class ChatRequest(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=8000)


@app.post("/api/chat")
def chat(req: ChatRequest):
    return StreamingResponse(
        ai.chat_stream(req.conversation_id, req.message),
        media_type="text/plain; charset=utf-8",
    )


@app.delete("/api/chat/{conversation_id}")
def chat_reset(conversation_id: str):
    ai.reset(conversation_id)
    return {"ok": True}
