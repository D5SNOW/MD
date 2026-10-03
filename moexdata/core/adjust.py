from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

PRICE_COLUMNS = ("open", "high", "low", "close")
RUB_CODES = {"RUB", "SUR", "RUR"}


@dataclass
class PriceEvent:
    ex_day: pd.Timestamp
    factor: float
    kind: str
    note: str


def _days(candles: pd.DataFrame) -> pd.Series:
    return pd.to_datetime(candles["begin"], errors="coerce").dt.normalize()


def corporate_events(
    candles: pd.DataFrame, dividends: pd.DataFrame, splits: pd.DataFrame
) -> list[PriceEvent]:
    if candles.empty or "begin" not in candles.columns:
        return []
    days = _days(candles)
    daily_close = candles.assign(_d=days).groupby("_d")["close"].last().sort_index()
    trading_days = daily_close.index
    events: list[PriceEvent] = []

    for row in (dividends if dividends is not None else pd.DataFrame()).itertuples(index=False):
        row = row._asdict()
        currency = str(row.get("currencyid") or "RUB").upper()
        value = row.get("value")
        last_buy = pd.Timestamp(row.get("last_buy_date"))
        if currency not in RUB_CODES or pd.isna(value) or value <= 0 or pd.isna(last_buy):
            continue
        after = trading_days[trading_days > last_buy]
        before = trading_days[trading_days <= last_buy]
        if after.empty or before.empty:
            continue
        prev_close = float(daily_close[before[-1]])
        if not 0 < value < prev_close:
            continue
        events.append(PriceEvent(
            after[0], 1 - value / prev_close, "dividend",
            f"дивиденд {value:g} ₽, реестр {pd.Timestamp(row.get('registryclosedate')).date()}, "
            f"гэп {after[0].date()}",
        ))

    for row in (splits if splits is not None else pd.DataFrame()).itertuples(index=False):
        row = row._asdict()
        before_qty, after_qty = row.get("before"), row.get("after")
        ex_day = pd.Timestamp(row.get("tradedate")).normalize()
        if not before_qty or not after_qty or pd.isna(ex_day):
            continue
        if ex_day <= trading_days[0] or ex_day > trading_days[-1]:
            continue
        events.append(PriceEvent(
            ex_day, float(before_qty) / float(after_qty), "split",
            f"сплит {before_qty:g}:{after_qty:g} с {ex_day.date()}",
        ))
    return sorted(events, key=lambda e: e.ex_day)


def apply_factors(candles: pd.DataFrame, events: list[PriceEvent]) -> pd.DataFrame:
    # Корректировка назад: последние цены совпадают с биржевыми.
    df = candles.copy()
    days = _days(df)
    factor = pd.Series(1.0, index=df.index)
    for event in events:
        factor[days < event.ex_day] *= event.factor
    df["adj_factor"] = factor.round(10)
    for column in PRICE_COLUMNS:
        if column in df.columns:
            df[f"adj_{column}"] = (pd.to_numeric(df[column], errors="coerce") * factor).round(6)
    return df


@dataclass
class RollGap:
    boundary: pd.Timestamp
    old: str
    new: str
    old_close: float
    new_close: float

    @property
    def diff(self) -> float:
        return self.new_close - self.old_close

    @property
    def ratio(self) -> float:
        return self.new_close / self.old_close


def apply_roll_gaps(candles: pd.DataFrame, gaps: list[RollGap], mode: str) -> pd.DataFrame:
    df = candles.copy()
    if mode not in ("diff", "ratio") or "begin" not in df.columns:
        return df
    stamp = pd.to_datetime(df["begin"], errors="coerce")
    if mode == "diff":
        shift = pd.Series(0.0, index=df.index)
        for gap in gaps:
            shift[stamp < gap.boundary] += gap.diff
        df["adj_shift"] = shift.round(6)
        for column in PRICE_COLUMNS:
            if column in df.columns:
                df[f"adj_{column}"] = (pd.to_numeric(df[column], errors="coerce") + shift).round(6)
    else:
        factor = pd.Series(1.0, index=df.index)
        for gap in gaps:
            if gap.old_close > 0 and gap.new_close > 0:
                factor[stamp < gap.boundary] *= gap.ratio
        df["adj_factor"] = factor.round(10)
        for column in PRICE_COLUMNS:
            if column in df.columns:
                df[f"adj_{column}"] = (pd.to_numeric(df[column], errors="coerce") * factor).round(6)
    return df
