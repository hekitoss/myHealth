"""Чат с Gemini по данным тренировок.

В системную инструкцию кладётся сводка выбранных тренировок (вид спорта + период) и недельные агрегаты.
Посекундный пульс конкретной тренировки модель запрашивает инструментом get_workout_hr.

Готовая системная инструкция кэшируется по выборке и сбрасывается при любом изменении данных
(db.data_version). Текст для одной выборки не меняется между вопросами, поэтому Gemini
переиспользует его через неявный кэш; при AI_CONTEXT_CACHE=explicit создаётся явный кэш Gemini.
"""
import json
import logging
import time
from datetime import date, timedelta

import httpx
from google import genai
from google.genai import errors, types

from . import db, metrics
from .config import AI_CONTEXT_CACHE, AI_MODEL, AI_THINKING, GEMINI_API_KEY

log = logging.getLogger(__name__)

MAX_CONVERSATIONS = 20
_conversations: dict[str, list] = {}

MAX_CACHED_SELECTIONS = 16
GEMINI_CACHE_TTL_S = 3600
_contexts: dict[tuple, str] = {}               # выборка -> системная инструкция
_gemini_caches: dict[tuple, tuple | None] = {}  # выборка -> (имя кэша Gemini, когда истекает) | None
_cache_version = None

INSTRUCTIONS = """Ты — помощник по анализу тренировок и пульса. Пользователь тренируется с нагрудным
датчиком Polar H9. Ниже его данные. Отвечай по-русски, опирайся на цифры из данных и называй
конкретные даты и тренировки. Если данных недостаточно для вывода — так и скажи.

В данных только тренировки из выборки, которую пользователь задал в интерфейсе (вид спорта и период).
Если вопрос про тренировки вне выборки — скажи, что их нужно добавить в выборку над чатом.
В разделе «Запланировано» (если он есть) — будущие тренировки из плана пользователя.

Для деталей пульса внутри тренировки (динамика, восстановление, дрейф, интервалы) вызывай
инструмент get_workout_hr с id тренировки.

Ты не врач: не ставь диагнозов. Если в данных есть что-то, что стоит показать врачу
(например, необычно высокий пульс в покое или резкие скачки), мягко порекомендуй это."""

TOOLS = [types.Tool(function_declarations=[types.FunctionDeclaration(
    name="get_workout_hr",
    description=(
        "Возвращает пульс одной тренировки, усреднённый по окнам step_s секунд, "
        "плюс итоговую статистику. Используй для вопросов о динамике пульса внутри тренировки."
    ),
    parameters_json_schema={
        "type": "object",
        "properties": {
            "workout_id": {"type": "integer", "description": "id тренировки из таблицы"},
            "step_s": {"type": "integer", "description": "размер окна усреднения, сек (по умолчанию 30)"},
        },
        "required": ["workout_id"],
    },
)])]


class AIError(Exception):
    pass


def is_configured():
    return bool(GEMINI_API_KEY)


def _selection_key(sports, days):
    return (tuple(sorted(set(sports))) if sports else (), days or None)


def build_context(sports=None, days=None):
    sports, days = _selection_key(sports, days)
    since = date.today() - timedelta(days=days) if days else None
    workouts = [
        w for w in reversed(db.list_workouts(since.isoformat() if since else None))  # по возрастанию даты
        if not sports or w["sport"] in sports
    ]
    lines = ["id,дата,время,спорт,мин,км,ср_пульс,макс_пульс,ккал,нагрузка,z1_мин,z2_мин,z3_мин,z4_мин,z5_мин"]
    for w in workouts:
        dt = w["start_time"]
        z = [round(s / 60) for s in (w["zones"] or [])] or [""] * 5
        lines.append(",".join(str(x) for x in [
            w["id"], dt[:10], dt[11:16], w["sport"] or "",
            round((w["duration_s"] or 0) / 60),
            round(w["distance_m"] / 1000, 2) if w["distance_m"] else "",
            w["avg_hr"] or "", w["max_hr"] or "", w["calories"] or "",
            w["training_load"] or "", *z,
        ]))
    weeks = min(26, -(-days // 7)) if days else 26
    weekly = metrics.weekly_summary(workouts, weeks=weeks)
    week_lines = ["неделя_с,тренировок,мин,нагрузка,ср_пульс,z1..z5_мин"]
    for wk in weekly:
        week_lines.append(f"{wk['week']},{wk['count']},{wk['duration_min']},{wk['load']},"
                          f"{wk['avg_hr'] or ''},{'/'.join(map(str, wk['zones_min']))}")
    scope = (", ".join(sports) if sports else "все виды спорта") + \
        (f"; последние {days} дн. (с {since.isoformat()})" if days else "; за всё время")
    return (
        f"## Выборка\n{scope}\n\n"
        f"## Профиль\nHRmax для зон: {db.hr_max()} уд/мин. Зоны: Z1 50–60%, Z2 60–70%, Z3 70–80%, "
        f"Z4 80–90%, Z5 90–100% HRmax.\n\n"
        f"## Тренировки ({len(workouts)} шт.)\n" + "\n".join(lines) +
        f"\n\n## По неделям (последние {weeks})\n" + "\n".join(week_lines) +
        _planned_context()
    )


def _planned_context():
    planned = db.list_planned(date.today().isoformat())
    if not planned:
        return ""
    lines = ["id,дата,спорт,название,мин,км,зона,пульс,темп,выполнено"]
    for p in planned:
        hr = "–".join(str(v) for v in (p["hr_low"], p["hr_high"]) if v)
        lines.append(",".join(str(x if x is not None else "").replace(",", ";") for x in [
            p["id"], p["date"], p["sport"], p["title"], p["duration_min"], p["distance_km"],
            p["zone"] and f"Z{p['zone']}", hr, p["pace"], "да" if p["done"] else "",
        ]))
    return f"\n\n## Запланировано (с сегодняшнего дня, {len(planned)} шт.)\n" + "\n".join(lines)


def _system_instruction(key):
    """Готовая системная инструкция для выборки: собирается один раз, пока не изменились данные."""
    global _cache_version
    version = (db.data_version(), date.today())  # «последние N дней» сдвигаются со сменой даты
    if version != _cache_version:
        _drop_caches()
        _cache_version = version
    if key not in _contexts:
        if len(_contexts) >= MAX_CACHED_SELECTIONS:
            _contexts.pop(next(iter(_contexts)))
        _contexts[key] = f"{INSTRUCTIONS}\n\n{build_context(*key)}\n\nСегодня {date.today().isoformat()}."
    return _contexts[key]


def _gemini_cache(client, key, system):
    """Имя явного кэша Gemini для выборки или None, если явный кэш выключен или недоступен."""
    if AI_CONTEXT_CACHE != "explicit":
        return None
    entry = _gemini_caches.get(key, ...)
    if entry is None or (entry is not ... and entry[1] > time.time()):
        return entry and entry[0]
    try:
        cache = client.caches.create(model=AI_MODEL, config=types.CreateCachedContentConfig(
            display_name="myhealth-context", system_instruction=system, tools=TOOLS,
            ttl=f"{GEMINI_CACHE_TTL_S}s",
        ))
        entry = (cache.name, time.time() + GEMINI_CACHE_TTL_S - 60)
    except errors.APIError as e:
        # Бесплатный тариф или слишком маленький контекст — работаем без явного кэша.
        log.warning("Явный кэш Gemini не создан (%s): %s", e.code, e.message)
        entry = None
    _gemini_caches[key] = entry
    return entry and entry[0]


def _drop_caches():
    _contexts.clear()
    stale = [e[0] for e in _gemini_caches.values() if e]
    _gemini_caches.clear()
    if stale and is_configured():
        client = genai.Client(api_key=GEMINI_API_KEY)
        for name in stale:
            try:
                client.caches.delete(name=name)
            except (errors.APIError, httpx.TransportError):
                pass  # истечёт сам по TTL


def _run_tool(name, args):
    if name != "get_workout_hr":
        return {"error": f"неизвестный инструмент {name}"}, True
    wid = _int_arg(args.get("workout_id"))
    step = _int_arg(args.get("step_s"), 30)
    if not isinstance(wid, int) or not isinstance(step, int) or step < 1:
        return {"error": "workout_id и step_s должны быть целыми положительными числами"}, True
    w = db.get_workout(wid)
    if not w:
        return {"error": f"тренировки {wid} нет"}, True
    if not w["hr"]:
        return {"error": "у этой тренировки нет посекундного пульса"}, True
    step = max(step, w["hr_rate_s"])
    return {
        "workout_id": wid,
        "start_time": w["start_time"],
        "sport": w["sport"],
        "step_s": step,
        "avg_hr": w["avg_hr"], "max_hr": w["max_hr"], "min_hr": w["min_hr"],
        "hr": metrics.downsample(w["hr"], w["hr_rate_s"], step),
    }, False


def _int_arg(value, default=None):
    """Gemini передаёт целые числа из JSON как float (30.0) — приводим обратно."""
    if value is None:
        return default
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _saved_history(conversation_id):
    """История сохранённой сессии (только текст) — чтобы продолжить её после перезапуска сервера."""
    saved = db.get_chat_session(conversation_id)
    return [
        types.Content(role="user" if m["role"] == "user" else "model", parts=[types.Part(text=m["text"])])
        for m in (saved["messages"] if saved else [])
    ]


def reset(conversation_id):
    _conversations.pop(conversation_id, None)


def chat_stream(conversation_id, user_message, sports=None, days=None):
    """Генератор кусков текста ответа. Историю диалога держим в памяти процесса.

    sports/days задают выборку тренировок для контекста (None — все виды / всё время).
    """
    if conversation_id not in _conversations and len(_conversations) >= MAX_CONVERSATIONS:
        _conversations.pop(next(iter(_conversations)))
    if conversation_id not in _conversations:
        _conversations[conversation_id] = _saved_history(conversation_id)
    contents = _conversations[conversation_id]
    user_turn = types.Content(role="user", parts=[types.Part(text=user_message)])
    contents.append(user_turn)

    if not is_configured():
        contents.pop()
        yield ("⚠️ Не найден ключ Gemini API. Получите бесплатный ключ на https://aistudio.google.com/apikey, "
               "добавьте GEMINI_API_KEY в файл .env и перезапустите приложение.")
        return

    client = genai.Client(api_key=GEMINI_API_KEY)
    key = _selection_key(sports, days)
    system = _system_instruction(key)
    cached = _gemini_cache(client, key, system)
    config = types.GenerateContentConfig(
        # С явным кэшем инструкция и инструменты уже лежат в нём, повторно их передавать нельзя.
        **({"cached_content": cached} if cached else {"system_instruction": system, "tools": TOOLS}),
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        thinking_config=types.ThinkingConfig(thinking_level=AI_THINKING),
        max_output_tokens=32000,
    )

    try:
        while True:
            parts, finish, usage = [], None, None
            for chunk in client.models.generate_content_stream(model=AI_MODEL, contents=contents, config=config):
                if not chunk.candidates:
                    continue
                usage = chunk.usage_metadata or usage
                cand = chunk.candidates[0]
                finish = cand.finish_reason or finish
                for part in (cand.content.parts if cand.content and cand.content.parts else []):
                    parts.append(part)  # сохраняем всё, включая thought_signature — Gemini требует их вернуть
                    if part.text and not part.thought:
                        yield part.text

            if usage:
                log.info("Gemini: промпт %s токенов, из кэша %s",
                         usage.prompt_token_count, usage.cached_content_token_count or 0)
            contents.append(types.Content(role="model", parts=parts))
            calls = [p.function_call for p in parts if p.function_call]

            if finish == types.FinishReason.MAX_TOKENS:
                yield "\n\n_Ответ обрезан по длине._"
                return
            if finish in (types.FinishReason.SAFETY, types.FinishReason.PROHIBITED_CONTENT,
                          types.FinishReason.BLOCKLIST):
                yield "\n\n_Модель отказалась отвечать на этот запрос. Попробуйте переформулировать._"
                return
            if not calls:
                if not any(p.text and not p.thought for p in parts):
                    yield "_Модель вернула пустой ответ, попробуйте ещё раз._"
                return

            results = []
            for call in calls:
                result, is_error = _run_tool(call.name, dict(call.args or {}))
                results.append(types.Part.from_function_response(
                    name=call.name,
                    response={"error": result["error"]} if is_error else {"output": result},
                ))
            contents.append(types.Content(role="user", parts=results))
            yield "\n\n"
    except (errors.APIError, httpx.TransportError) as e:
        _rollback(contents, user_turn)
        yield f"⚠️ {_error_text(e)}"
    except Exception as e:  # иначе поток молча обрывается и в чате пусто
        log.exception("Ошибка в чате с AI")
        _rollback(contents, user_turn)
        yield f"⚠️ Внутренняя ошибка: {type(e).__name__}: {e}"


def _error_text(e):
    if isinstance(e, httpx.TransportError):
        return "Нет соединения с Gemini API."
    if e.code in (401, 403) or (e.code == 400 and "API key" in (e.message or "")):
        return "Ключ Gemini API не подошёл. Проверьте GEMINI_API_KEY в .env."
    if e.code == 429:
        return "Исчерпан бесплатный лимит запросов к Gemini API, попробуйте через минуту (или завтра, если кончился дневной)."
    return f"Ошибка Gemini API ({e.code}): {e.message}"


def _rollback(contents, user_turn):
    """Убирает незавершённый ход, чтобы следующий вопрос не сломал историю."""
    while contents and contents[-1] is not user_turn:
        contents.pop()
    if contents:
        contents.pop()


# --- план тренировок под цель --------------------------------------------------

PLAN_HISTORY_DAYS = 180
PLAN_SPORTS = ["RUNNING", "TREADMILL_RUNNING", "CYCLING", "INDOOR_CYCLING", "STRENGTH_TRAINING",
               "SWIMMING", "WALKING", "HIKING", "YOGA", "OTHER"]

PLAN_INSTRUCTIONS = """Ты — тренер по выносливости. Составь план тренировок под цель пользователя, опираясь
на его историю ниже: текущий недельный объём, частоту, виды спорта, средний пульс и время в зонах.
Начинай с объёма, близкого к текущему, наращивай его не больше чем на 10% в неделю, чередуй тяжёлые
и лёгкие дни, оставляй дни отдыха, каждую 3–4-ю неделю и неделю перед стартом делай разгрузочной.
Большую часть объёма — в Z1–Z2. Тренировки из раздела «Запланировано» уже стоят в плане:
не ставь новые на эти даты и учитывай их в объёме.

Для каждой тренировки укажи дату в заданном периоде, вид спорта кодом из списка, короткое название
(«Лёгкий бег», «Интервалы 5×1 км»), длительность, дистанцию — если она важна, целевую зону и диапазон
пульса по HRmax пользователя, темп для бега (мм:сс на км) — только если его можно оценить по истории,
и в notes — структуру тренировки (разминка, основная часть, заминка) одной-тремя фразами.

В summary по-русски, до 6 предложений, объясни логику плана и на какие цифры из истории ты опирался.
Если истории мало — так и скажи и составь осторожный план. Ты не врач: не ставь диагнозов."""


def _plan_schema(sports):
    item = {
        "type": "object",
        "properties": {
            "date": {"type": "string", "format": "date", "description": "YYYY-MM-DD"},
            "sport": {"type": "string", "enum": sports},
            "title": {"type": "string"},
            "duration_min": {"type": "integer"},
            "distance_km": {"type": "number"},
            "zone": {"type": "integer", "minimum": 1, "maximum": 5},
            "hr_low": {"type": "integer"},
            "hr_high": {"type": "integer"},
            "pace": {"type": "string", "description": "мм:сс на км"},
            "notes": {"type": "string"},
        },
        "required": ["date", "sport", "title", "duration_min"],
    }
    return {
        "type": "object",
        "properties": {"summary": {"type": "string"}, "workouts": {"type": "array", "items": item}},
        "required": ["summary", "workouts"],
    }


def generate_plan(goal, start, weeks, per_week, sports=None):
    """Черновик плана от Gemini: {"summary", "workouts"}. Ничего не сохраняет — пользователь сначала смотрит."""
    if not is_configured():
        raise AIError("Не найден ключ Gemini API: добавьте GEMINI_API_KEY в файл .env и перезапустите приложение.")
    end = start + timedelta(days=weeks * 7 - 1)
    history = {w["sport"] for w in db.list_workouts() if w["sport"]}
    allowed = PLAN_SPORTS + sorted(history - set(PLAN_SPORTS))
    wanted = [s for s in sports or [] if s in allowed]
    prompt = (
        f"Цель: {goal}\n"
        f"Период плана: с {start.isoformat()} по {end.isoformat()} ({weeks} нед.), "
        f"примерно {per_week} тренировок в неделю.\n"
        f"Виды спорта: {', '.join(wanted) if wanted else 'выбери сам по истории и цели'}."
    )
    system = (f"{PLAN_INSTRUCTIONS}\n\n{build_context(None, PLAN_HISTORY_DAYS)}\n\n"
              f"Сегодня {date.today().isoformat()}.")
    client = genai.Client(api_key=GEMINI_API_KEY)
    try:
        resp = client.models.generate_content(model=AI_MODEL, contents=prompt, config=types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_json_schema=_plan_schema(allowed),
            thinking_config=types.ThinkingConfig(thinking_level=AI_THINKING),
            max_output_tokens=32000,
        ))
    except (errors.APIError, httpx.TransportError) as e:
        raise AIError(_error_text(e)) from e
    try:
        data = json.loads(resp.text or "")
    except ValueError:
        log.warning("План от Gemini не разобран: %r", (resp.text or "")[:500])
        raise AIError("Модель вернула план в неожиданном формате, попробуйте ещё раз.")
    workouts = [w for w in (_plan_item(raw, start, end) for raw in data.get("workouts") or []) if w]
    if not workouts:
        raise AIError("Модель не предложила ни одной тренировки, попробуйте уточнить цель.")
    workouts.sort(key=lambda w: w["date"])
    return {"summary": str(data.get("summary") or ""), "workouts": workouts}


def _plan_item(raw, start, end):
    """Тренировка из ответа модели с проверкой полей; None — если дата вне периода или не разобрана."""
    if not isinstance(raw, dict):
        return None
    try:
        day = date.fromisoformat(str(raw.get("date", ""))[:10])
    except ValueError:
        return None
    if not start <= day <= end:
        return None

    def num(key, lo, hi, digits=None):
        v = raw.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            return None
        return round(v, digits) if digits else int(round(v))

    def text(key, limit):
        return str(raw.get(key) or "").strip()[:limit] or None

    return {
        "date": day.isoformat(), "sport": text("sport", 40) or "OTHER", "title": text("title", 120),
        "duration_min": num("duration_min", 1, 1440), "distance_km": num("distance_km", 0.1, 1000, 2),
        "zone": num("zone", 1, 5), "hr_low": num("hr_low", 40, 230), "hr_high": num("hr_high", 40, 230),
        "pace": text("pace", 20), "notes": text("notes", 2000),
    }
