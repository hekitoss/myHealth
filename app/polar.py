"""Клиент Polar AccessLink API v3: OAuth2, регистрация пользователя, загрузка тренировок.

Документация: https://www.polar.com/accesslink-api/
Ограничение API: /v3/exercises отдаёт только тренировки, загруженные в Polar Flow
за последние 30 дней и после подключения приложения. Историю импортируйте файлами.
"""
from urllib.parse import urlencode

import requests

from . import db, metrics
from .config import POLAR_CLIENT_ID, POLAR_CLIENT_SECRET, POLAR_REDIRECT_URI

AUTH_URL = "https://flow.polar.com/oauth2/authorization"
TOKEN_URL = "https://polarremote.com/v2/oauth2/token"
API_URL = "https://www.polaraccesslink.com/v3"

SAMPLE_TYPE_HR = "0"  # sample-type "0" — пульс, уд/мин


class PolarError(Exception):
    pass


def is_configured():
    return bool(POLAR_CLIENT_ID and POLAR_CLIENT_SECRET)


def is_connected(user):
    return bool(db.get_setting(user, "polar_token"))


def authorization_url(state):
    params = {
        "response_type": "code",
        "client_id": POLAR_CLIENT_ID,
        "redirect_uri": POLAR_REDIRECT_URI,
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def connect(user, code):
    """Меняет code на токен и регистрирует аккаунт Polar в нашем клиенте AccessLink.

    Токен сохраняется у пользователя приложения user: у каждого свой аккаунт Polar.
    """
    r = requests.post(
        TOKEN_URL,
        auth=(POLAR_CLIENT_ID, POLAR_CLIENT_SECRET),
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": POLAR_REDIRECT_URI},
        headers={"Accept": "application/json"},
        timeout=30,
    )
    if r.status_code != 200:
        raise PolarError(f"Не удалось получить токен: {r.status_code} {r.text}")
    tok = r.json()
    token, user_id = tok["access_token"], tok["x_user_id"]

    r = requests.post(
        f"{API_URL}/users",
        json={"member-id": f"myhealth-{user_id}"},
        headers=_headers(token),
        timeout=30,
    )
    if r.status_code not in (200, 201, 409):  # 409 — пользователь уже зарегистрирован
        raise PolarError(f"Не удалось зарегистрировать пользователя: {r.status_code} {r.text}")

    db.set_setting(user, "polar_token", token)
    db.set_setting(user, "polar_user_id", user_id)


def disconnect(user):
    db.delete_setting(user, "polar_token")
    db.delete_setting(user, "polar_user_id")


def _headers(token):
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def sync(user):
    """Скачивает доступные тренировки с пульсом и зонами. Возвращает {'new': n, 'updated': m}."""
    token = db.get_setting(user, "polar_token")
    if not token:
        raise PolarError("Polar не подключён")
    r = requests.get(
        f"{API_URL}/exercises",
        params={"samples": "true", "zones": "true"},
        headers=_headers(token),
        timeout=60,
    )
    if r.status_code == 401:
        disconnect(user)
        raise PolarError("Токен Polar недействителен, подключитесь заново")
    if r.status_code == 204:
        return {"new": 0, "updated": 0}
    if r.status_code != 200:
        raise PolarError(f"Ошибка AccessLink: {r.status_code} {r.text}")

    new = updated = 0
    for ex in r.json():
        workout, hr, rate = _map_exercise(ex)
        _, is_new = db.upsert_workout(user, workout, hr, rate)
        new += is_new
        updated += not is_new
    return {"new": new, "updated": updated}


def _map_exercise(ex):
    hr_info = ex.get("heart_rate") or {}
    zones = None
    if ex.get("heart_rate_zones"):
        ordered = sorted(ex["heart_rate_zones"], key=lambda z: z.get("index", 0))
        zones = [metrics.parse_iso_duration(z.get("in-zone")) or 0 for z in ordered][:5]

    hr, rate = None, 1
    for s in ex.get("samples") or []:
        if str(s.get("sample-type")) == SAMPLE_TYPE_HR and s.get("data"):
            rate = int(s.get("recording-rate") or 1)
            hr = [_to_int(v) for v in s["data"].split(",")]
            break

    workout = {
        "source": "polar",
        "external_id": ex["id"],
        "start_time": ex["start_time"],
        "sport": ex.get("detailed_sport_info") or ex.get("sport"),
        "duration_s": metrics.parse_iso_duration(ex.get("duration")),
        "distance_m": ex.get("distance"),
        "calories": ex.get("calories"),
        "avg_hr": hr_info.get("average"),
        "max_hr": hr_info.get("maximum"),
        "training_load": ex.get("training_load"),
        "zones": zones,
    }
    return workout, hr, rate


def _to_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0
