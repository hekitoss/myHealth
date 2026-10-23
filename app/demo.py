"""Генератор демо-тренировок, чтобы посмотреть интерфейс без подключения Polar."""
import math
import random
from datetime import datetime, timedelta

from . import db

SPORTS = [
    # спорт, длительность мин, базовый пульс, амплитуда, интервальная?
    ("RUNNING", (30, 60), 148, 12, False),
    ("RUNNING", (35, 50), 152, 25, True),
    ("CYCLING", (45, 100), 135, 15, False),
    ("STRENGTH_TRAINING", (40, 70), 118, 25, True),
    ("WALKING", (30, 70), 102, 8, False),
]


def generate(days=90, seed=42):
    rnd = random.Random(seed)
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    created = 0
    fitness = 0.0  # пульс на той же нагрузке постепенно снижается
    for d in range(days, -1, -1):
        if rnd.random() > 0.55:
            continue
        sport, (lo, hi), base, amp, intervals = rnd.choice(SPORTS)
        minutes = rnd.randint(lo, hi)
        start = today - timedelta(days=d) + timedelta(hours=rnd.choice([7, 8, 18, 19]), minutes=rnd.randint(0, 59))
        if start + timedelta(minutes=minutes) > datetime.now():
            continue
        base_today = base - fitness + rnd.uniform(-4, 4)
        hr = _hr_series(rnd, minutes * 60, base_today, amp, intervals)
        db.upsert_workout({
            "source": "demo",
            "external_id": start.isoformat(),
            "start_time": start.isoformat(timespec="seconds"),
            "sport": sport,
            "duration_s": minutes * 60,
            "distance_m": _distance(rnd, sport, minutes),
            "calories": int(minutes * (base_today - 60) * 0.11),
            "training_load": round(minutes * max(base_today - 90, 5) / 40, 1),
        }, hr, 1)
        created += 1
        fitness = min(fitness + 0.08, 8)
    return created


def _hr_series(rnd, seconds, base, amp, intervals):
    hr, rest = [], 72
    warmup = min(300, seconds // 6)
    cooldown = min(240, seconds // 8)
    for t in range(seconds):
        if t < warmup:
            target = rest + (base - rest) * (t / warmup)
        elif t > seconds - cooldown:
            target = base - (base - rest - 15) * ((t - (seconds - cooldown)) / cooldown)
        else:
            drift = 6 * (t / seconds)  # кардиодрифт
            if intervals:
                target = base + drift + (amp if (t // 180) % 2 else -amp / 2)
            else:
                target = base + drift + amp * 0.3 * math.sin(t / 240)
        prev = hr[-1] if hr else rest
        hr.append(round(prev + (target - prev) * 0.05 + rnd.uniform(-1.2, 1.2)))
    return hr


def _distance(rnd, sport, minutes):
    speed = {"RUNNING": 10.5, "CYCLING": 25, "WALKING": 5.2}.get(sport)
    return round(speed * minutes / 60 * 1000 * rnd.uniform(0.9, 1.1)) if speed else None
