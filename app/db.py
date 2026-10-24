"""SQLite-хранилище тренировок, пульсовых записей и настроек."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from . import metrics
from .config import DB_PATH, DEFAULT_HR_MAX

SCHEMA = """
CREATE TABLE IF NOT EXISTS workouts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,          -- polar | tcx | csv | demo
    external_id   TEXT NOT NULL,
    start_time    TEXT NOT NULL,          -- локальное время, ISO 8601
    sport         TEXT,
    duration_s    INTEGER,
    distance_m    REAL,
    calories      INTEGER,
    avg_hr        INTEGER,
    max_hr        INTEGER,
    min_hr        INTEGER,
    training_load REAL,
    zones_json    TEXT,                   -- секунды в зонах 1..5
    created_at    TEXT DEFAULT (datetime('now')),
    UNIQUE (source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_workouts_start ON workouts(start_time);

CREATE TABLE IF NOT EXISTS hr_samples (
    workout_id INTEGER PRIMARY KEY REFERENCES workouts(id) ON DELETE CASCADE,
    rate_s     INTEGER NOT NULL,          -- интервал между значениями, сек
    data       TEXT NOT NULL              -- пульс через запятую
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS chat_sessions (
    id          TEXT PRIMARY KEY,         -- conversation_id из интерфейса
    title       TEXT NOT NULL,            -- начало первого вопроса
    sports_json TEXT,                     -- выборка последнего вопроса: виды спорта
    days        INTEGER,                  --   и период, дней (NULL — всё время)
    hidden      INTEGER NOT NULL DEFAULT 0, -- 1 — удалена из интерфейса, но хранится в базе
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_sessions_updated ON chat_sessions(updated_at);

CREATE TABLE IF NOT EXISTS chat_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role       TEXT NOT NULL,             -- user | ai
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages(session_id, id);

CREATE TABLE IF NOT EXISTS planned_workouts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    date         TEXT NOT NULL,           -- YYYY-MM-DD
    sport        TEXT NOT NULL,           -- код вида спорта, как в workouts (RUNNING, CYCLING…)
    title        TEXT,
    duration_min INTEGER,
    distance_km  REAL,
    zone         INTEGER,                 -- целевая пульсовая зона 1..5
    hr_low       INTEGER,                 -- целевой пульс, уд/мин
    hr_high      INTEGER,
    pace         TEXT,                    -- целевой темп, мм:сс на км
    notes        TEXT,
    goal         TEXT,                    -- цель плана, если тренировку предложил AI
    done         INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_planned_date ON planned_workouts(date);
"""


_data_version = 0


def data_version():
    """Растёт при каждом изменении тренировок — по нему сбрасывается кэш контекста AI."""
    return _data_version


def _touch():
    global _data_version
    _data_version += 1


@contextmanager
def session():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init():
    with session() as c:
        c.executescript(SCHEMA)
        # База, созданная до появления мягкого удаления сессий чата.
        if "hidden" not in {r["name"] for r in c.execute("PRAGMA table_info(chat_sessions)")}:
            c.execute("ALTER TABLE chat_sessions ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")


# --- настройки ---------------------------------------------------------------

def get_setting(key, default=None):
    with session() as c:
        row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_setting(key, value):
    with session() as c:
        c.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )


def delete_setting(key):
    with session() as c:
        c.execute("DELETE FROM settings WHERE key = ?", (key,))


def hr_max():
    return int(get_setting("hr_max", DEFAULT_HR_MAX))


# --- тренировки --------------------------------------------------------------

def upsert_workout(w, hr=None, rate_s=1):
    """Сохраняет тренировку (dict с полями таблицы) и пульс. Возвращает (id, is_new)."""
    if hr:
        stats = metrics.hr_stats(hr)
        for k in ("avg_hr", "max_hr", "min_hr"):
            if not w.get(k):
                w[k] = stats[k]
        if not w.get("zones"):
            w["zones"] = metrics.zone_seconds(hr, rate_s, hr_max())
    fields = {
        "source": w["source"],
        "external_id": str(w["external_id"]),
        "start_time": w["start_time"],
        "sport": w.get("sport"),
        "duration_s": w.get("duration_s"),
        "distance_m": w.get("distance_m"),
        "calories": w.get("calories"),
        "avg_hr": w.get("avg_hr"),
        "max_hr": w.get("max_hr"),
        "min_hr": w.get("min_hr"),
        "training_load": w.get("training_load"),
        "zones_json": json.dumps(w["zones"]) if w.get("zones") else None,
    }
    with session() as c:
        existing = c.execute(
            "SELECT id FROM workouts WHERE source = ? AND external_id = ?",
            (fields["source"], fields["external_id"]),
        ).fetchone()
        if existing:
            wid = existing["id"]
            sets = ", ".join(f"{k} = :{k}" for k in fields)
            c.execute(f"UPDATE workouts SET {sets} WHERE id = :id", {**fields, "id": wid})
        else:
            cols = ", ".join(fields)
            vals = ", ".join(f":{k}" for k in fields)
            wid = c.execute(f"INSERT INTO workouts({cols}) VALUES({vals})", fields).lastrowid
        if hr:
            c.execute(
                "INSERT INTO hr_samples(workout_id, rate_s, data) VALUES(?, ?, ?) "
                "ON CONFLICT(workout_id) DO UPDATE SET rate_s = excluded.rate_s, data = excluded.data",
                (wid, rate_s, ",".join(str(int(v)) for v in hr)),
            )
    _touch()
    return wid, existing is None


def _row_to_dict(row):
    d = dict(row)
    d["zones"] = json.loads(d.pop("zones_json")) if d.get("zones_json") else None
    return d


def list_workouts(since=None):
    q = "SELECT w.*, (h.workout_id IS NOT NULL) AS has_hr FROM workouts w " \
        "LEFT JOIN hr_samples h ON h.workout_id = w.id"
    args = ()
    if since:
        q += " WHERE w.start_time >= ?"
        args = (since,)
    q += " ORDER BY w.start_time DESC"
    with session() as c:
        return [_row_to_dict(r) for r in c.execute(q, args).fetchall()]


def get_workout(wid):
    with session() as c:
        row = c.execute("SELECT * FROM workouts WHERE id = ?", (wid,)).fetchone()
        if not row:
            return None
        w = _row_to_dict(row)
        s = c.execute("SELECT rate_s, data FROM hr_samples WHERE workout_id = ?", (wid,)).fetchone()
    w["hr_rate_s"] = s["rate_s"] if s else None
    w["hr"] = [int(v) for v in s["data"].split(",")] if s else []
    return w


def delete_source(source):
    with session() as c:
        deleted = c.execute("DELETE FROM workouts WHERE source = ?", (source,)).rowcount
    _touch()
    return deleted


def recompute_zones():
    """Пересчитывает зоны по пульсу после смены HRmax (кроме зон, пришедших из Polar)."""
    limit = hr_max()
    with session() as c:
        rows = c.execute(
            "SELECT w.id, h.rate_s, h.data FROM workouts w JOIN hr_samples h ON h.workout_id = w.id "
            "WHERE w.source != 'polar'"
        ).fetchall()
        for r in rows:
            hr = [int(v) for v in r["data"].split(",")]
            c.execute(
                "UPDATE workouts SET zones_json = ? WHERE id = ?",
                (json.dumps(metrics.zone_seconds(hr, r["rate_s"], limit)), r["id"]),
            )
    _touch()


def count_workouts():
    with session() as c:
        return c.execute("SELECT COUNT(*) FROM workouts").fetchone()[0]


# --- сессии чата с AI --------------------------------------------------------

def save_chat_turn(session_id, question, answer, sports=None, days=None):
    """Дописывает вопрос и ответ в сессию; первая пара создаёт сессию."""
    now = datetime.now().isoformat(timespec="seconds")
    title = " ".join(question.split())
    title = title if len(title) <= 80 else title[:79].rstrip() + "…"
    with session() as c:
        c.execute(
            "INSERT INTO chat_sessions(id, title, sports_json, days, created_at, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
            "sports_json = excluded.sports_json, days = excluded.days, updated_at = excluded.updated_at",
            (session_id, title, json.dumps(sports or []), days, now, now),
        )
        c.executemany(
            "INSERT INTO chat_messages(session_id, role, text, created_at) VALUES(?, ?, ?, ?)",
            [(session_id, "user", question, now), (session_id, "ai", answer, now)],
        )


def list_chat_sessions():
    with session() as c:
        rows = c.execute(
            "SELECT s.*, (SELECT COUNT(*) FROM chat_messages m WHERE m.session_id = s.id AND m.role = 'user') "
            "AS questions FROM chat_sessions s WHERE s.hidden = 0 ORDER BY s.updated_at DESC"
        ).fetchall()
    return [_chat_session_dict(r) for r in rows]


def get_chat_session(session_id):
    with session() as c:
        row = c.execute("SELECT * FROM chat_sessions WHERE id = ?", (session_id,)).fetchone()
        if not row:
            return None
        msgs = c.execute(
            "SELECT role, text, created_at FROM chat_messages WHERE session_id = ? ORDER BY id", (session_id,)
        ).fetchall()
    s = _chat_session_dict(row)
    s["messages"] = [dict(m) for m in msgs]
    return s


def hide_chat_session(session_id):
    """Мягкое удаление: сессия остаётся в базе, но не показывается в списке."""
    with session() as c:
        return c.execute("UPDATE chat_sessions SET hidden = 1 WHERE id = ?", (session_id,)).rowcount


def _chat_session_dict(row):
    d = dict(row)
    d["sports"] = json.loads(d.pop("sports_json") or "[]")
    return d


# --- планируемые тренировки --------------------------------------------------
# Входят в контекст AI, поэтому каждое изменение сбрасывает его кэш (_touch).

PLANNED_FIELDS = ("date", "sport", "title", "duration_min", "distance_km", "zone",
                  "hr_low", "hr_high", "pace", "notes", "goal", "done")


def list_planned(since=None):
    q, args = "SELECT * FROM planned_workouts", ()
    if since:
        q, args = q + " WHERE date >= ?", (since,)
    with session() as c:
        rows = c.execute(q + " ORDER BY date, id", args).fetchall()
    return [_planned_dict(r) for r in rows]


def get_planned(pid):
    with session() as c:
        row = c.execute("SELECT * FROM planned_workouts WHERE id = ?", (pid,)).fetchone()
    return _planned_dict(row) if row else None


def add_planned(items):
    now = datetime.now().isoformat(timespec="seconds")
    cols = ", ".join(PLANNED_FIELDS)
    vals = ", ".join(f":{k}" for k in PLANNED_FIELDS)
    with session() as c:
        ids = [
            c.execute(f"INSERT INTO planned_workouts({cols}, created_at, updated_at) VALUES({vals}, :now, :now)",
                      {**{k: it.get(k) for k in PLANNED_FIELDS}, "now": now}).lastrowid
            for it in items
        ]
    _touch()
    return ids


def update_planned(pid, item):
    sets = ", ".join(f"{k} = :{k}" for k in PLANNED_FIELDS)
    with session() as c:
        n = c.execute(f"UPDATE planned_workouts SET {sets}, updated_at = :now WHERE id = :id", {
            **{k: item.get(k) for k in PLANNED_FIELDS},
            "now": datetime.now().isoformat(timespec="seconds"), "id": pid,
        }).rowcount
    _touch()
    return n


def delete_planned(pid):
    with session() as c:
        n = c.execute("DELETE FROM planned_workouts WHERE id = ?", (pid,)).rowcount
    _touch()
    return n


def _planned_dict(row):
    d = dict(row)
    d["done"] = bool(d["done"])
    return d
