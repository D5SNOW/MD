from __future__ import annotations

from datetime import date, timedelta
from typing import Callable


def split_range(
    start: date,
    end: date,
    chunk_days: int,
    skip_weekends: bool = True,
) -> list[tuple[date, date]]:
    if start > end:
        raise ValueError("Дата начала не может быть позже даты окончания")
    if chunk_days <= 0:
        return [(start, end)]

    chunks: list[tuple[date, date]] = []
    cursor = start
    step = timedelta(days=chunk_days)
    while cursor <= end:
        chunk_end = min(cursor + step - timedelta(days=1), end)
        piece = (cursor, chunk_end)
        if not skip_weekends or _has_business_day(*piece):
            chunks.append(_trim_weekends(*piece) if skip_weekends else piece)
        cursor = chunk_end + timedelta(days=1)
    return chunks


def _has_business_day(start: date, end: date) -> bool:
    day = start
    while day <= end:
        if day.weekday() < 5:
            return True
        day += timedelta(days=1)
    return False


def _trim_weekends(start: date, end: date) -> tuple[date, date]:
    while start <= end and start.weekday() >= 5:
        start += timedelta(days=1)
    while end >= start and end.weekday() >= 5:
        end -= timedelta(days=1)
    return start, end


def drop_non_trading(
    chunks: list[tuple[date, date]],
    is_trading: Callable[[date], bool | None],
) -> list[tuple[date, date]]:
    def maybe(day: date) -> bool:
        return is_trading(day) is not False

    result = []
    for start, end in chunks:
        while start <= end and not maybe(start):
            start += timedelta(days=1)
        while end >= start and not maybe(end):
            end -= timedelta(days=1)
        if start <= end:
            result.append((start, end))
    return result


def describe_plan(chunks: list[tuple[date, date]]) -> str:
    if not chunks:
        return "нет запросов"
    if len(chunks) == 1:
        return f"1 запрос ({chunks[0][0]} — {chunks[0][1]})"
    span = (chunks[0][1] - chunks[0][0]).days + 1
    return f"{len(chunks)} запрос(ов) по {span} дн."
