from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

# Москва живёт по UTC+3 без перехода на летнее время, tzdata не нужна.
MOSCOW_TZ = timezone(timedelta(hours=3), "MSK")

# Метрики Algopack за день досчитываются после закрытия вечерней сессии.
SETTLE_HOUR = 3


def moscow_now() -> datetime:
    return datetime.now(MOSCOW_TZ)


def moscow_today() -> date:
    return moscow_now().date()


def is_settled(day: date, now: datetime | None = None) -> bool:
    now = now or moscow_now()
    return day < (now - timedelta(hours=SETTLE_HOUR)).date()
