"""Чат с Gemini по данным тренировок.

В системную инструкцию кладётся сводка выбранных тренировок (вид спорта + период) и недельные агрегаты.
Посекундный пульс конкретной тренировки модель запрашивает инструментом get_workout_hr.

Готовая системная инструкция кэшируется по выборке и сбрасывается при любом изменении данных
(db.data_version). Текст для одной выборки не меняется между вопросами, поэтому Gemini
переиспользует его через неявный кэш; при AI_CONTEXT_CACHE=explicit создаётся явный кэш Gemini.
"""
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
        f"\n\n## По неделям (последние {weeks})\n" + "\n".join(week_lines)
    )


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


def reset(conversation_id):
    _conversations.pop(conversation_id, None)


def chat_stream(conversation_id, user_message, sports=None, days=None):
    """Генератор кусков текста ответа. Историю диалога держим в памяти процесса.

    sports/days задают выборку тренировок для контекста (None — все виды / всё время).
    """
    if conversation_id not in _conversations and len(_conversations) >= MAX_CONVERSATIONS:
        _conversations.pop(next(iter(_conversations)))
    contents = _conversations.setdefault(conversation_id, [])
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
    except errors.ClientError as e:
        _rollback(contents, user_turn)
        if e.code in (401, 403) or (e.code == 400 and "API key" in (e.message or "")):
            yield "⚠️ Ключ Gemini API не подошёл. Проверьте GEMINI_API_KEY в .env."
        elif e.code == 429:
            yield "⚠️ Исчерпан бесплатный лимит запросов к Gemini API, попробуйте через минуту (или завтра, если кончился дневной)."
        else:
            yield f"⚠️ Ошибка Gemini API ({e.code}): {e.message}"
    except errors.APIError as e:
        _rollback(contents, user_turn)
        yield f"⚠️ Ошибка Gemini API ({e.code}): {e.message}"
    except httpx.TransportError:
        _rollback(contents, user_turn)
        yield "⚠️ Нет соединения с Gemini API."
    except Exception as e:  # иначе поток молча обрывается и в чате пусто
        log.exception("Ошибка в чате с AI")
        _rollback(contents, user_turn)
        yield f"⚠️ Внутренняя ошибка: {type(e).__name__}: {e}"


def _rollback(contents, user_turn):
    """Убирает незавершённый ход, чтобы следующий вопрос не сломал историю."""
    while contents and contents[-1] is not user_turn:
        contents.pop()
    if contents:
        contents.pop()
