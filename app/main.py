"""Веб-сервер: API для интерфейса, OAuth Polar, чат с AI."""
import itertools
import secrets
from datetime import date as Date, datetime, timedelta

from fastapi import Cookie, Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import ai, db, demo, importer, metrics, polar
from .config import DEFAULT_USER, STATIC_DIR, USERS

db.init()
app = FastAPI(title="MyHealth")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def no_stale_frontend(request, call_next):
    """Браузер перепроверяет index.html/app.js/style.css при каждой загрузке — иначе после
    обновления кода может взять старый index.html к новому app.js, и интерфейс ломается."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response

_oauth_states: dict[str, str] = {}  # state OAuth -> пользователь, который подключает Polar


def current_user(user: str | None = Cookie(default=None)) -> str:
    """Пользователь из cookie «user», которую ставит переключатель в интерфейсе."""
    return user if user in USERS else DEFAULT_USER


User = Depends(current_user)


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/status")
def status(user: str = User):
    return {
        "user": user,
        "users": [{"id": k, "name": v} for k, v in USERS.items()],
        "polar_configured": polar.is_configured(),
        "polar_connected": polar.is_connected(user),
        "ai_configured": ai.is_configured(),
        "workouts": db.count_workouts(user),
        "hr_max": db.hr_max(user),
        "last_sync": db.get_setting(user, "last_sync"),
    }


@app.get("/api/workouts")
def workouts(days: int | None = None, user: str = User):
    since = (datetime.now() - timedelta(days=days)).isoformat() if days else None
    return db.list_workouts(user, since)


@app.get("/api/workouts/{wid}")
def workout(wid: int, user: str = User):
    w = db.get_workout(user, wid)
    if not w:
        raise HTTPException(404, "Тренировка не найдена")
    return w


@app.get("/api/weekly")
def weekly(weeks: int = 12, user: str = User):
    return metrics.weekly_summary(db.list_workouts(user), weeks)


class Settings(BaseModel):
    hr_max: int = Field(ge=120, le=230)


@app.put("/api/settings")
def update_settings(s: Settings, user: str = User):
    db.set_setting(user, "hr_max", s.hr_max)
    db.recompute_zones(user)
    return {"ok": True}


# --- план тренировок ----------------------------------------------------------

class PlannedWorkout(BaseModel):
    date: Date
    sport: str = Field(min_length=1, max_length=40)
    title: str | None = Field(default=None, max_length=120)
    duration_min: int | None = Field(default=None, ge=1, le=1440)
    distance_km: float | None = Field(default=None, gt=0, le=1000)
    zone: int | None = Field(default=None, ge=1, le=5)
    hr_low: int | None = Field(default=None, ge=40, le=230)
    hr_high: int | None = Field(default=None, ge=40, le=230)
    pace: str | None = Field(default=None, max_length=20)
    notes: str | None = Field(default=None, max_length=2000)
    goal: str | None = Field(default=None, max_length=300)
    done: bool = False


@app.get("/api/planned")
def planned(user: str = User):
    return db.list_planned(user)


@app.post("/api/planned")
def planned_add(p: PlannedWorkout, user: str = User):
    return db.get_planned(user, db.add_planned(user, [p.model_dump(mode="json")])[0])


@app.post("/api/planned/bulk")
def planned_add_many(items: list[PlannedWorkout], user: str = User):
    if len(items) > 200:
        raise HTTPException(400, "Слишком много тренировок за раз")
    return {"ids": db.add_planned(user, [p.model_dump(mode="json") for p in items])}


@app.put("/api/planned/{pid}")
def planned_update(pid: int, p: PlannedWorkout, user: str = User):
    if not db.update_planned(user, pid, p.model_dump(mode="json")):
        raise HTTPException(404, "Тренировка не найдена")
    return db.get_planned(user, pid)


@app.delete("/api/planned/{pid}")
def planned_delete(pid: int, user: str = User):
    return {"deleted": db.delete_planned(user, pid)}


class PlanRequest(BaseModel):
    goal: str = Field(min_length=3, max_length=300)
    start: Date
    weeks: int = Field(ge=1, le=16)
    per_week: int = Field(ge=1, le=14)
    sports: list[str] = Field(default=[], max_length=10)


@app.post("/api/planned/generate")
def planned_generate(req: PlanRequest, user: str = User):
    try:
        return ai.generate_plan(user, req.goal, req.start, req.weeks, req.per_week, req.sports)
    except ai.AIError as e:
        raise HTTPException(e.status, str(e))


# --- Polar -------------------------------------------------------------------

@app.get("/polar/connect")
def polar_connect(user: str = User):
    if not polar.is_configured():
        raise HTTPException(400, "Заполните POLAR_CLIENT_ID и POLAR_CLIENT_SECRET в .env")
    state = secrets.token_urlsafe(16)
    _oauth_states[state] = user
    return RedirectResponse(polar.authorization_url(state))


@app.get("/oauth2_callback")
def polar_callback(code: str | None = None, state: str | None = None, error: str | None = None):
    if error or not code:
        return RedirectResponse(f"/?polar=error&msg={error or 'no_code'}")
    # Пользователь — тот, кто начал подключение, а не текущая cookie: её могли переключить в другой вкладке.
    user = _oauth_states.pop(state or "", None)
    if not user:
        return RedirectResponse("/?polar=error&msg=bad_state")
    try:
        polar.connect(user, code)
    except polar.PolarError:
        return RedirectResponse("/?polar=error&msg=connect_failed")
    return RedirectResponse("/?polar=connected")


@app.post("/api/polar/disconnect")
def polar_disconnect(user: str = User):
    polar.disconnect(user)
    return {"ok": True}


@app.post("/api/sync")
def sync(user: str = User):
    try:
        result = polar.sync(user)
    except polar.PolarError as e:
        raise HTTPException(400, str(e))
    db.set_setting(user, "last_sync", datetime.now().isoformat(timespec="seconds"))
    return result


# --- импорт и демо -----------------------------------------------------------

@app.post("/api/import")
async def import_files(files: list[UploadFile] = File(...), user: str = User):
    new = updated = 0
    errors = []
    for f in files:
        try:
            _, is_new = importer.import_file(user, f.filename or "", await f.read())
            new += is_new
            updated += not is_new
        except (importer.ImportErrorBadFile, KeyError, ValueError) as e:
            errors.append(f"{f.filename}: {e}")
    return {"new": new, "updated": updated, "errors": errors}


@app.post("/api/demo")
def demo_create(user: str = User):
    return {"created": demo.generate(user)}


@app.delete("/api/demo")
def demo_delete(user: str = User):
    return {"deleted": db.delete_source(user, "demo")}


# --- AI ----------------------------------------------------------------------

class ChatRequest(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=8000)
    # Выборка тренировок для контекста: виды спорта (пусто — все) и последние N дней (None — всё время).
    sports: list[str] = Field(default=[], max_length=50)
    days: int | None = Field(default=None, ge=1, le=3650)


@app.post("/api/chat")
def chat(req: ChatRequest, user: str = User):
    chunks = ai.chat_stream(user, req.conversation_id, req.message, req.sports, req.days)
    # Ждём первый кусок до отправки заголовков: если Gemini сразу вернул ошибку (а так бывает почти всегда),
    # отвечаем её же HTTP-статусом, чтобы код в логе сервера и в интерфейсе совпадал.
    first = next(chunks, "")
    if isinstance(first, ai.ChatError):
        return PlainTextResponse(first, status_code=first.status)

    def stream():
        parts, failed = [], False
        for chunk in itertools.chain([first], chunks):
            # Ошибка посреди ответа: статус 200 уже отправлен, интерфейс поймёт по метке.
            failed = failed or chunk.startswith(ai.ERROR_MARK)
            parts.append(chunk)
            yield chunk
        # Сохраняем только завершённый ответ без ошибки: при ошибке или обрыве соединения
        # ai.py убирает вопрос из истории, и в базе его тоже быть не должно.
        answer = "".join(parts).strip()
        if answer and not failed:
            db.save_chat_turn(user, req.conversation_id, req.message, answer, req.sports, req.days)

    return StreamingResponse(stream(), media_type="text/plain; charset=utf-8")


@app.get("/api/chat/sessions")
def chat_sessions(user: str = User):
    return db.list_chat_sessions(user)


@app.get("/api/chat/sessions/{session_id}")
def chat_session(session_id: str, user: str = User):
    s = db.get_chat_session(user, session_id)
    if not s:
        raise HTTPException(404, "Сессия не найдена")
    return s


@app.delete("/api/chat/sessions/{session_id}")
def chat_session_delete(session_id: str, user: str = User):
    ai.reset(user, session_id)
    return {"hidden": db.hide_chat_session(user, session_id)}


@app.delete("/api/chat/{conversation_id}")
def chat_reset(conversation_id: str, user: str = User):
    ai.reset(user, conversation_id)
    return {"ok": True}
