from __future__ import annotations

import threading
from datetime import date, timedelta
from typing import Any

import pandas as pd

from moexdata.api.datasets import DATASETS, MARKETS, DatasetInfo, source_period
from moexdata.errors import ApiKeyError, classify
from moexdata.timeutil import moscow_today

_lock = threading.Lock()
_current_key: str | None = None
_ticker_cache: dict[tuple[str, str], Any] = {}


def configure(api_key: str) -> None:
    from moexalgo import session

    key = (api_key or "").strip()
    if not key:
        raise ApiKeyError("API-ключ Algopack не задан.")
    global _current_key
    with _lock:
        if key != _current_key:
            session.TOKEN = key
            _current_key = key
            _ticker_cache.clear()


def reset() -> None:
    from moexalgo import session

    global _current_key
    with _lock:
        session.TOKEN = None
        _current_key = None
        _ticker_cache.clear()


def check_api_key(api_key: str) -> tuple[bool, str]:
    try:
        configure(api_key)
    except ApiKeyError as exc:
        return False, str(exc)

    from moexalgo import Market

    try:
        market = Market("shares")
        probe = market.tradestats(date=_last_business_day().isoformat(), latest=True)
        rows = 0 if probe is None else len(probe)
        return True, f"Ключ принят. Тестовый запрос вернул строк: {rows}."
    except Exception as exc:  # noqa: BLE001
        err = classify(exc)
        return False, str(err)


def _last_business_day(today: date | None = None) -> date:
    day = today or moscow_today()
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


_TICKER_FIELDS = {
    "futures": ("secid", "shortname", "assetcode", "lasttradedate"),
}
_DEFAULT_TICKER_FIELDS = ("secid", "shortname")


def list_tickers(market_key: str, api_key: str) -> pd.DataFrame:
    configure(api_key)
    from moexalgo import Market

    info = MARKETS[market_key]
    fields = _TICKER_FIELDS.get(market_key, _DEFAULT_TICKER_FIELDS)
    df = Market(info.key).tickers(*fields)
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=["secid", "shortname"])
    if not isinstance(df, pd.DataFrame):
        df = pd.DataFrame(list(df))
    if "secid" not in df.columns and "ticker" in df.columns:
        df = df.rename(columns={"ticker": "secid"})
    if "shortname" not in df.columns:
        df["shortname"] = ""
    df = df.dropna(subset=["secid"])
    return df.sort_values("secid").reset_index(drop=True)


def get_ticker(market_key: str, secid: str, api_key: str) -> Any:
    configure(api_key)
    from moexalgo import Ticker

    cache_key = (market_key, secid)
    with _lock:
        cached = _ticker_cache.get(cache_key)
    if cached is not None:
        return cached

    board = MARKETS[market_key].board
    try:
        ticker = Ticker(secid, board=board)
    except LookupError:
        ticker = Ticker(secid)
    with _lock:
        _ticker_cache[cache_key] = ticker
    return ticker


def fetch_chunk(
    market_key: str,
    secid: str,
    dataset_key: str,
    start: date,
    end: date,
    period: str,
    api_key: str,
) -> pd.DataFrame:
    ticker = get_ticker(market_key, secid, api_key)
    dataset = DATASETS[dataset_key]
    result = dataset.fetch(ticker, start, end, period)
    return _as_frame(result)


def _as_frame(result: Any) -> pd.DataFrame:
    if result is None:
        return pd.DataFrame()
    if not isinstance(result, pd.DataFrame):
        result = pd.DataFrame(list(result))
    return result


def trading_calendar(market_key: str, start: date, end: date, api_key: str):
    configure(api_key)
    from moexdata.api.calendar import load_calendar

    return load_calendar(market_key, start, end)


def dividends(secid: str, api_key: str) -> pd.DataFrame:
    configure(api_key)
    from moexdata.api.corporate import fetch_dividends

    return fetch_dividends(secid)


def splits(secid: str, api_key: str) -> pd.DataFrame:
    configure(api_key)
    from moexdata.api.corporate import fetch_splits

    return fetch_splits(secid)


_SERIES_DATE_COLUMNS = ("last_trade_date", "lasttradedate", "expiration_date", "lastdeldate")
# underlying_asset в серии - это USD000UTSTOM, а не код актива Si.
_SERIES_ASSET_COLUMNS = ("asset_code", "assetcode", "asset")


def futures_contracts(name: str, api_key: str) -> tuple[str, list]:
    configure(api_key)
    from moexdata.core.continuous import Contract

    active = list_tickers("futures", api_key)
    asset = name
    if "assetcode" in active.columns:
        row = active[active["secid"].str.upper() == name.upper()]
        if not row.empty and row["assetcode"].iloc[0]:
            asset = str(row["assetcode"].iloc[0])

    contracts: list[Contract] = []
    try:
        contracts = _expired_series(asset)
    except Exception:  # noqa: BLE001
        contracts = []

    if "assetcode" in active.columns and "lasttradedate" in active.columns:
        own = active[active["assetcode"].astype(str).str.upper() == asset.upper()]
        for secid, last in zip(own["secid"], own["lasttradedate"]):
            parsed = _to_date(last)
            if parsed is not None:
                contracts.append(Contract(str(secid), parsed))
    return asset, contracts


def _expired_series(asset: str) -> list:
    from moexdata.api import iss
    from moexdata.core.continuous import Contract

    raw, _ = iss.fetch(
        "statistics/engines/futures/markets/forts/series",
        {"asset_code": asset, "show_expired": 1},
    )
    result: list[Contract] = []
    for block in raw.values():
        if not isinstance(block, dict) or "columns" not in block:
            continue
        columns = [str(c).lower() for c in block["columns"]]
        date_col = next((c for c in _SERIES_DATE_COLUMNS if c in columns), None)
        if "secid" not in columns or date_col is None:
            continue
        asset_col = next((c for c in _SERIES_ASSET_COLUMNS if c in columns), None)
        idx = {c: i for i, c in enumerate(columns)}
        for row in block.get("data") or []:
            if asset_col and str(row[idx[asset_col]] or "").upper() != asset.upper():
                continue
            parsed = _to_date(row[idx[date_col]])
            if parsed is not None:
                result.append(Contract(str(row[idx["secid"]]), parsed))
    return result


def _to_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


HISTORY_FLOOR = date(1997, 1, 1)

HISTORY_PROBE_PERIOD = "1D"


def detect_history_range(
    market_key: str,
    secid: str,
    dataset_key: str,
    api_key: str,
    period: str = "",
) -> tuple[date, date] | None:
    ticker = get_ticker(market_key, secid, api_key)
    dataset = DATASETS[dataset_key]
    today = moscow_today()
    if dataset.requires_period:
        period = source_period(period) if period else HISTORY_PROBE_PERIOD
    else:
        period = ""

    # ISS отдаёт записи от начала диапазона, поэтому первый ответ начинается
    # с самой ранней даты, а ответ с latest заканчивается самой поздней.
    first = _probe_dates(dataset, ticker, today, period, latest=False)
    if first is None:
        return None
    start = first[0]

    end = first[1]
    if dataset.supports_latest:
        last = _probe_dates(dataset, ticker, today, period, latest=True)
        if last is not None:
            end = last[1]
    else:
        end = today

    return start, min(max(end, start), today)


def _probe_dates(
    dataset: DatasetInfo,
    ticker: Any,
    today: date,
    period: str,
    latest: bool,
) -> tuple[date, date] | None:
    frame = _as_frame(dataset.fetch(ticker, HISTORY_FLOOR, today, period, latest=latest))
    if frame.empty:
        return None
    for column in dataset.time_columns:
        if column not in frame.columns:
            continue
        values = pd.to_datetime(frame[column], errors="coerce").dropna()
        if values.empty:
            continue
        return values.min().date(), values.max().date()
    return None
