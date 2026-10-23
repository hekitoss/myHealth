"""Расчёты по пульсу: статистика, зоны, недельные агрегаты."""
import re
from collections import defaultdict
from datetime import date, datetime, timedelta

# Границы зон в % от HRmax, как в Polar: 50-60, 60-70, 70-80, 80-90, 90-100
ZONE_BOUNDS = (0.5, 0.6, 0.7, 0.8, 0.9)


def hr_stats(hr):
    valid = [v for v in hr if v > 0]
    if not valid:
        return {"avg_hr": None, "max_hr": None, "min_hr": None}
    return {
        "avg_hr": round(sum(valid) / len(valid)),
        "max_hr": max(valid),
        "min_hr": min(valid),
    }


def zone_seconds(hr, rate_s, hr_max):
    zones = [0] * 5
    for v in hr:
        pct = v / hr_max
        for i in range(4, -1, -1):
            if pct >= ZONE_BOUNDS[i]:
                zones[i] += rate_s
                break
    return zones


def downsample(hr, rate_s, step_s):
    """Средние значения пульса по окнам step_s секунд."""
    n = max(1, step_s // max(rate_s, 1))
    out = []
    for i in range(0, len(hr), n):
        chunk = [v for v in hr[i:i + n] if v > 0]
        out.append(round(sum(chunk) / len(chunk)) if chunk else None)
    return out


_ISO_DUR = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?")


def parse_iso_duration(s):
    """'PT1H2M3.5S' -> 3723 секунды."""
    m = _ISO_DUR.fullmatch(s or "")
    if not m:
        return None
    d, h, mi, sec = m.groups()
    return int(int(d or 0) * 86400 + int(h or 0) * 3600 + int(mi or 0) * 60 + float(sec or 0))


def week_start(iso):
    d = datetime.fromisoformat(iso).date()
    return d - timedelta(days=d.weekday())


def weekly_summary(workouts, weeks=12):
    """Недельные агрегаты за последние `weeks` недель (включая пустые недели)."""
    agg = defaultdict(lambda: {"count": 0, "duration_s": 0, "load": 0.0, "hr_weighted": 0, "zones": [0] * 5})
    for w in workouts:
        a = agg[week_start(w["start_time"])]
        a["count"] += 1
        a["duration_s"] += w["duration_s"] or 0
        a["load"] += w["training_load"] or 0
        if w["avg_hr"] and w["duration_s"]:
            a["hr_weighted"] += w["avg_hr"] * w["duration_s"]
        for i, z in enumerate(w["zones"] or []):
            a["zones"][i] += z
    this_week = date.today() - timedelta(days=date.today().weekday())
    out = []
    for k in range(weeks - 1, -1, -1):
        ws = this_week - timedelta(weeks=k)
        a = agg.get(ws, {"count": 0, "duration_s": 0, "load": 0.0, "hr_weighted": 0, "zones": [0] * 5})
        out.append({
            "week": ws.isoformat(),
            "count": a["count"],
            "duration_min": round(a["duration_s"] / 60),
            "load": round(a["load"]),
            "avg_hr": round(a["hr_weighted"] / a["duration_s"]) if a["duration_s"] and a["hr_weighted"] else None,
            "zones_min": [round(z / 60) for z in a["zones"]],
        })
    return out
