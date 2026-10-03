from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

QUARTER_MONTHS = {3, 6, 9, 12}


@dataclass(frozen=True)
class Contract:
    secid: str
    last_trade_date: date


@dataclass(frozen=True)
class Segment:
    start: date
    end: date
    secid: str


def roll_date(expiry: date, roll_days: int) -> date:
    day = expiry
    left = max(0, roll_days)
    while left:
        day -= timedelta(days=1)
        if day.weekday() < 5:
            left -= 1
    return day


def select_contracts(contracts: list[Contract], quarterly_only: bool) -> list[Contract]:
    unique = {c.secid: c for c in contracts}
    result = sorted(unique.values(), key=lambda c: (c.last_trade_date, c.secid))
    if quarterly_only:
        quarterly = [c for c in result if c.last_trade_date.month in QUARTER_MONTHS]
        if quarterly:
            result = quarterly
    return result


def build_schedule(
    contracts: list[Contract], start: date, end: date, roll_days: int
) -> list[Segment]:
    segments: list[Segment] = []
    cursor = start
    for contract in contracts:
        if cursor > end:
            break
        last = roll_date(contract.last_trade_date, roll_days)
        if last < cursor:
            continue
        seg_end = min(last, end)
        segments.append(Segment(cursor, seg_end, contract.secid))
        cursor = seg_end + timedelta(days=1)
    # После последнего перехода держим самый дальний контракт до конца периода.
    if cursor <= end and contracts:
        tail = contracts[-1]
        if segments and segments[-1].secid == tail.secid:
            segments[-1] = Segment(segments[-1].start, end, tail.secid)
        else:
            segments.append(Segment(cursor, end, tail.secid))
    return segments


def output_name(asset: str) -> str:
    return f"{asset}_cont"
