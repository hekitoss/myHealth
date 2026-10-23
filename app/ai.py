"""Чат с Gemini по данным тренировок.

В системную инструкцию кладётся сводка всех тренировок и недельные агрегаты.
Посекундный пульс конкретной тренировки модель запрашивает инструментом get_workout_hr.
"""
import json
from datetime import date

import httpx
from google import genai
from google.genai import errors, types

from . import db, metrics
from .config import AI_MODEL, AI_THINKING, GEMINI_API_KEY

MAX_CONVERSATIONS = 20
_conversations: dict[str, list] = {}

INSTRUCTIONS = """Ты — помощник по анализу тренировок и пульса. Пользователь тренируется с нагрудным
датчиком Polar H9. Ниже его данные. Отвечай по-русски, опирайся на цифры из данных и называй
конкретные даты и тренировки. Если данных недостаточно для вывода — так и скажи.

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


def build_context():
    workouts = list(reversed(db.list_workouts()))  # по возрастанию даты
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
    weekly = metrics.weekly_summary([w for w in workouts], weeks=26)
    week_lines = ["неделя_с,тренировок,мин,нагрузка,ср_пульс,z1..z5_мин"]
    for wk in weekly:
        week_lines.append(f"{wk['week']},{wk['count']},{wk['duration_min']},{wk['load']},"
                          f"{wk['avg_hr'] or ''},{'/'.join(map(str, wk['zones_min']))}")
    return (
        f"## Профиль\nHRmax для зон: {db.hr_max()} уд/мин. Зоны: Z1 50–60%, Z2 60–70%, Z3 70–80%, "
        f"Z4 80–90%, Z5 90–100% HRmax.\n\n"
        f"## Тренировки ({len(workouts)} шт.)\n" + "\n".join(lines) +
        "\n\n## По неделям (последние 26)\n" + "\n".join(week_lines)
    )


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


def chat_stream(conversation_id, user_message):
    """Генератор кусков текста ответа. Историю диалога держим в памяти процесса."""
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
    config = types.GenerateContentConfig(
        system_instruction=f"{INSTRUCTIONS}\n\n{build_context()}\n\nСегодня {date.today().isoformat()}.",
        tools=TOOLS,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        thinking_config=types.ThinkingConfig(thinking_level=AI_THINKING),
        max_output_tokens=32000,
    )

    try:
        while True:
            parts, finish = [], None
            for chunk in client.models.generate_content_stream(model=AI_MODEL, contents=contents, config=config):
                if not chunk.candidates:
                    continue
                cand = chunk.candidates[0]
                finish = cand.finish_reason or finish
                for part in (cand.content.parts if cand.content and cand.content.parts else []):
                    parts.append(part)  # сохраняем всё, включая thought_signature — Gemini требует их вернуть
                    if part.text and not part.thought:
                        yield part.text

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


def _rollback(contents, user_turn):
    """Убирает незавершённый ход, чтобы следующий вопрос не сломал историю."""
    while contents and contents[-1] is not user_turn:
        contents.pop()
    if contents:
        contents.pop()
