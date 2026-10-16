"""Use the same naive Japan timestamps as the existing workout database."""
from datetime import datetime, timedelta, timezone

JAPAN_TIME = timezone(timedelta(hours=9))


def japan_now():
    return datetime.now(JAPAN_TIME).replace(tzinfo=None)


def japan_today():
    return japan_now().date()
