from datetime import timedelta
from zoneinfo import ZoneInfo


def move_weekend_to_monday(value):
    return value + timedelta(days=7 - value.weekday()) if value.weekday() >= 5 else value


def due_at(original, timezone, days):
    return move_weekend_to_monday(original.astimezone(ZoneInfo(timezone)) + timedelta(days=days))


def calculate_followup1_due(original, timezone):
    return due_at(original, timezone, 3)


def calculate_followup2_due(original, timezone):
    return due_at(original, timezone, 7)


def is_due(now, due):
    return now >= due
