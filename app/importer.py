"""Импорт тренировок из файлов, выгруженных из Polar Flow: TCX (в т.ч. .gz) и CSV.

Нужен для истории старше 30 дней, которую AccessLink API не отдаёт.
"""
import csv
import gzip
import io
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

from . import db

TCX_NS = {"t": "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"}


class ImportErrorBadFile(Exception):
    pass


def import_file(user, name, content):
    """Импортирует один файл. Возвращает (id, is_new)."""
    if content[:2] == b"\x1f\x8b":
        content = gzip.decompress(content)
        name = name.removesuffix(".gz")
    lower = name.lower()
    if lower.endswith(".tcx"):
        workout, hr = parse_tcx(content)
    elif lower.endswith(".csv"):
        workout, hr = parse_polar_csv(content.decode("utf-8-sig"))
    else:
        raise ImportErrorBadFile("поддерживаются только .tcx, .tcx.gz и .csv")
    return db.upsert_workout(user, workout, hr, 1)


def parse_tcx(content):
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        raise ImportErrorBadFile(f"Некорректный TCX: {e}")
    act = root.find(".//t:Activity", TCX_NS)
    if act is None:
        raise ImportErrorBadFile("В TCX нет тренировки (Activity)")

    start = _parse_ts(act.findtext("t:Id", namespaces=TCX_NS))
    duration = distance = calories = 0.0
    for lap in act.findall("t:Lap", TCX_NS):
        duration += float(lap.findtext("t:TotalTimeSeconds", "0", TCX_NS))
        distance += float(lap.findtext("t:DistanceMeters", "0", TCX_NS))
        calories += float(lap.findtext("t:Calories", "0", TCX_NS))

    points = []
    for tp in act.iterfind(".//t:Trackpoint", TCX_NS):
        bpm = tp.findtext("t:HeartRateBpm/t:Value", namespaces=TCX_NS)
        ts = tp.findtext("t:Time", namespaces=TCX_NS)
        if bpm and ts:
            points.append((_parse_ts(ts), int(bpm)))

    workout = {
        "source": "tcx",
        "external_id": start.isoformat(),
        "start_time": start.isoformat(timespec="seconds"),
        "sport": (act.get("Sport") or "OTHER").upper(),
        "duration_s": int(duration) or None,
        "distance_m": distance or None,
        "calories": int(calories) or None,
    }
    return workout, _resample_1s(points, start)


def parse_polar_csv(text):
    """CSV-экспорт сессии Polar Flow: строка заголовков, строка итогов, затем посекундные данные."""
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 3:
        raise ImportErrorBadFile("CSV слишком короткий")
    summary = dict(zip(rows[0], rows[1]))
    sample_header = rows[2]
    try:
        hr_col = sample_header.index("HR (bpm)")
    except ValueError:
        raise ImportErrorBadFile("В CSV нет колонки 'HR (bpm)' — это точно экспорт Polar Flow?")

    start = datetime.strptime(f"{summary['Date']} {summary['Start time']}", "%d-%m-%Y %H:%M:%S")
    hr = []
    for r in rows[3:]:
        try:
            hr.append(int(float(r[hr_col])))
        except (IndexError, ValueError):
            hr.append(hr[-1] if hr else 0)

    workout = {
        "source": "csv",
        "external_id": start.isoformat(),
        "start_time": start.isoformat(timespec="seconds"),
        "sport": (summary.get("Sport") or "OTHER").upper(),
        "duration_s": _hms_to_s(summary.get("Total time")),
        "distance_m": _float(summary.get("Distance (km)"), 1000),
        "calories": int(_float(summary.get("Calories")) or 0) or None,
        "training_load": _float(summary.get("Training load")),
    }
    return workout, hr


def _resample_1s(points, start):
    """Приводит пульс с произвольными метками времени к ряду по 1 секунде (последнее значение)."""
    if not points:
        return None
    points.sort()
    total = int((points[-1][0] - start).total_seconds()) + 1
    out, j, last = [], 0, points[0][1]
    for sec in range(max(total, 1)):
        t = start + timedelta(seconds=sec)
        while j < len(points) and points[j][0] <= t:
            last = points[j][1]
            j += 1
        out.append(last)
    return out


def _parse_ts(s):
    # TCX хранит время в UTC; переводим в локальное время компьютера
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt


def _hms_to_s(s):
    try:
        h, m, sec = (int(x) for x in s.split(":"))
        return h * 3600 + m * 60 + sec
    except (AttributeError, ValueError):
        return None


def _float(s, mult=1):
    try:
        return float(s) * mult
    except (TypeError, ValueError):
        return None
