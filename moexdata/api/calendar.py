from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Iterable

CALENDAR_ENGINE = {
    "shares": "stock",
    "bonds": "stock",
    "index": "stock",
    "futures": "futures",
    "options": "futures",
    "currency": "currency",
}

_ENGINE_ALIASES = {
    "stock": ("stock", "fond", "shares", "equity"),
    "futures": ("futures", "forts", "srochn", "derivatives"),
    "currency": ("currency", "selt", "valut", "fx"),
}

_DATE_COLUMNS = ("tradedate", "trade_date", "date", "day")
_TRADED_HINTS = ("is_traded", "traded", "workday", "is_work", "trading")

_MAX_PAGES = 50


@dataclass
class TradingCalendar:
    engine: str
    start: date
    end: date
    days: dict[date, bool] = field(default_factory=dict)
    reasons: dict[date, str] = field(default_factory=dict)
    source: str = ""

    def covers(self, day: date) -> bool:
        return self.start <= day <= self.end

    def is_trading(self, day: date) -> bool | None:
        if not self.covers(day):
            return None
        if day in self.days:
            return self.days[day]
        # Без show_all_days ISS перечисляет только исключения.
        return day.weekday() < 5

    def to_frame(self):
        import pandas as pd

        rows = []
        day = self.start
        while day <= self.end:
            rows.append({
                "date": day.isoformat(),
                "is_trading": bool(self.is_trading(day)),
                "reason": self.reasons.get(day, ""),
                "weekday": day.isoweekday(),
                "listed": day in self.days,
            })
            day += timedelta(days=1)
        return pd.DataFrame(rows)

    def summary(self) -> dict:
        frame = self.to_frame()
        weekend = frame["weekday"] >= 6
        return {
            "engine": self.engine,
            "source": self.source,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "trading_days": int(frame["is_trading"].sum()),
            "weekend_sessions": int((frame["is_trading"] & weekend).sum()),
            "weekday_holidays": int((~frame["is_trading"] & ~weekend).sum()),
        }

    def trading_days(self, start: date, end: date) -> list[date]:
        result = []
        day = max(start, self.start)
        last = min(end, self.end)
        while day <= last:
            if self.is_trading(day):
                result.append(day)
            day += timedelta(days=1)
        return result


def parse_calendar(raw: dict[str, Any], engine: str, start: date, end: date) -> TradingCalendar | None:
    best: tuple[int, dict[date, bool], dict[date, str]] | None = None
    for name, block in (raw or {}).items():
        parsed = _parse_block(str(name), block, engine)
        if parsed is None:
            continue
        score, days, reasons = parsed
        if best is None or score > best[0]:
            best = (score, days, reasons)
    if best is None:
        return None
    _, days, reasons = best
    days = {d: v for d, v in days.items() if start <= d <= end}
    reasons = {d: v for d, v in reasons.items() if start <= d <= end}
    return TradingCalendar(engine, start, end, days, reasons)


def _parse_block(
    name: str, block: Any, engine: str
) -> tuple[int, dict[date, bool], dict[date, str]] | None:
    if not isinstance(block, dict) or "columns" not in block or "data" not in block:
        return None
    columns = [str(c).lower() for c in block["columns"]]
    rows = block["data"] or []
    date_col = next((c for c in _DATE_COLUMNS if c in columns), None)
    if date_col is None:
        return None

    aliases = _ENGINE_ALIASES.get(engine, (engine,))
    # tradedate тоже содержит "traded".
    traded_cols = [
        c for c in columns
        if c not in _DATE_COLUMNS and any(h in c for h in _TRADED_HINTS)
    ]
    own = [c for c in traded_cols if any(a in c for a in aliases)]
    foreign = {a for other, names in _ENGINE_ALIASES.items() if other != engine for a in names}
    generic = [c for c in traded_cols if not any(a in c for a in foreign)]
    traded_col = (own or generic or [None])[0]
    if traded_col is None:
        return None

    engine_col = next(
        (c for c in columns if c in ("engine", "market", "engine_name", "market_name")), None
    )
    reasons = [c for c in columns if "reason" in c]
    reason_col = next((c for c in reasons if any(a in c for a in aliases)), None) or next(
        (c for c in reasons if not any(a in c for a in foreign)), None
    )

    idx = {c: i for i, c in enumerate(columns)}
    days: dict[date, bool] = {}
    reasons: dict[date, str] = {}
    for row in rows:
        if engine_col is not None:
            value = str(row[idx[engine_col]] or "").lower()
            if value and not any(a in value for a in aliases):
                continue
        day = _to_date(row[idx[date_col]])
        traded = _to_bool(row[idx[traded_col]])
        if day is None or traded is None:
            continue
        days[day] = traded
        if reason_col is not None and row[idx[reason_col]] is not None:
            reasons[day] = str(row[idx[reason_col]])

    if not days:
        return None
    score = 2 if own else 1
    if any(a in name.lower() for a in aliases):
        score += 2
    if engine_col is not None:
        score += 1
    return score, days, reasons


def _to_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _to_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in ("1", "true", "yes", "y", "да"):
        return True
    if text in ("0", "false", "no", "n", "нет"):
        return False
    return None


class CalendarTruncated(Exception):
    pass


def fetch_raw(start: date, end: date, host: str = "algopack") -> list[dict[str, Any]]:
    from moexdata.api import iss

    pages: list[dict[str, Any]] = []
    offset = 0
    for _ in range(_MAX_PAGES):
        page = iss.get_json(
            "calendars",
            {"from": start.isoformat(), "till": end.isoformat(), "show_all_days": 1, "start": offset},
            host,
        )
        # Сервер может игнорировать start и отдавать ту же страницу.
        if pages and page == pages[-1]:
            return pages
        pages.append(page)
        size = max(
            (len(b.get("data") or []) for b in page.values() if isinstance(b, dict)),
            default=0,
        )
        if size < 100:
            return pages
        offset += size
    raise CalendarTruncated(f"{start} — {end}")


def merge_pages(pages: Iterable[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for page in pages:
        for name, block in (page or {}).items():
            if not isinstance(block, dict) or "data" not in block:
                continue
            target = merged.setdefault(name, {"columns": block.get("columns", []), "data": []})
            target["data"].extend(block.get("data") or [])
    return merged


MAX_CALENDAR_YEARS = 10


def load_calendar(market_key: str, start: date, end: date) -> TradingCalendar | None:
    engine = CALENDAR_ENGINE.get(market_key)
    if engine is None:
        return None
    start = max(start, date(end.year - MAX_CALENDAR_YEARS, 1, 1))

    from moexdata.api import iss

    result: TradingCalendar | None = None
    host: str | None = None
    window_end = end
    while window_end >= start:
        window_start = max(start, date(window_end.year, 1, 1))
        part = None
        errors: list[Exception] = []
        for candidate in ([host] if host else list(iss.HOSTS)):
            try:
                pages = fetch_raw(window_start, window_end, candidate)
            except CalendarTruncated:
                pages = None
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                continue
            if pages is None:
                break
            part = parse_calendar(merge_pages(pages), engine, window_start, window_end)
            if part is not None:
                host = candidate
                part.source = candidate
                break
        if part is None and result is None and errors and len(errors) == len(iss.HOSTS):
            raise errors[-1]
        if part is None:
            if result is None:
                return None
            break
        if result is None:
            result = part
        else:
            result.days.update(part.days)
            result.reasons.update(part.reasons)
            result.start = part.start
        window_end = window_start - timedelta(days=1)
    return result
