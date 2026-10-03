from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

# Больше строк moexalgo за один вызов не отдаёт.
ROW_LIMIT = 10_000


@dataclass(frozen=True)
class MarketInfo:
    key: str
    label: str
    board: str
    algopack: bool
    futoi: bool = False


MARKETS: dict[str, MarketInfo] = {
    "shares": MarketInfo("shares", "Акции (TQBR)", "TQBR", True),
    "futures": MarketInfo("futures", "Фьючерсы (RFUD)", "RFUD", True, futoi=True),
    "currency": MarketInfo("currency", "Валюта (CETS)", "CETS", True),
    "index": MarketInfo("index", "Индексы (SNDX)", "SNDX", False),
    "bonds": MarketInfo("bonds", "Облигации (TQOB)", "TQOB", False),
    "options": MarketInfo("options", "Опционы (ROPD)", "ROPD", False),
}

MARKET_LABELS: dict[str, str] = {info.label: key for key, info in MARKETS.items()}


CANDLE_PERIODS: list[tuple[str, str, int, bool]] = [
    ("1min", "1 минута", 1, True),
    ("5min", "5 минут", 5, True),
    ("10min", "10 минут", 10, True),
    ("15min", "15 минут", 15, True),
    ("20min", "20 минут", 20, True),
    ("30min", "30 минут", 30, True),
    ("1h", "1 час", 60, True),
    ("2h", "2 часа", 120, True),
    ("3h", "3 часа", 120, True),
    ("6h", "6 часов", 180, True),
    ("12h", "12 часов", 180, True),
    ("1D", "1 день", 1825, True),
    ("5D", "5 дней", 0, False),
    ("10D", "10 дней", 0, False),
    ("1W", "1 неделя", 0, False),
    ("2W", "2 недели", 0, False),
    ("4W", "4 недели", 0, False),
    ("1M", "1 месяц", 0, False),
]

PERIOD_BY_KEY = {key: (label, chunk, splittable) for key, label, chunk, splittable in CANDLE_PERIODS}
PERIOD_LABELS = {label: key for key, label, _, _ in CANDLE_PERIODS}


def period_chunk_days(period: str) -> int:
    return PERIOD_BY_KEY.get(period, ("", 1, True))[1]


def period_is_splittable(period: str) -> bool:
    return PERIOD_BY_KEY.get(period, ("", 1, True))[2]


# Из каких свечей ISS moexalgo собирает нестандартные периоды.
_SOURCE_PERIOD = {
    "1min": "1min", "5min": "1min", "15min": "1min",
    "10min": "10min", "20min": "10min", "30min": "10min",
    "1h": "1h", "2h": "1h", "3h": "1h", "6h": "1h", "12h": "1h",
    "1D": "1D", "5D": "1D", "10D": "1D",
    "1W": "1W", "2W": "1W", "4W": "1W",
    "1M": "1M",
}


def source_period(period: str) -> str:
    return _SOURCE_PERIOD.get(period, "1D")


FetchFn = Callable[..., Any]


def _fetch_candles(ticker: Any, start: date, end: date, period: str, latest: bool = False):
    return ticker.candles(start=start, end=end, period=period or "1D", latest=latest)


def _fetch_tradestats(ticker: Any, start: date, end: date, period: str, latest: bool = False):
    return ticker.tradestats(start=start, end=end, latest=latest)


def _fetch_orderstats(ticker: Any, start: date, end: date, period: str, latest: bool = False):
    return ticker.orderstats(start=start, end=end, latest=latest)


def _fetch_obstats(ticker: Any, start: date, end: date, period: str, latest: bool = False):
    return ticker.obstats(start=start, end=end, latest=latest)


def _fetch_hi2(ticker: Any, start: date, end: date, period: str, latest: bool = False):
    return ticker.hi2(start=start, end=end, latest=latest)


def _fetch_alerts(ticker: Any, start: date, end: date, period: str, latest: bool = False):
    return ticker.alerts(start=start, end=end, latest=latest)


def _fetch_dividends(ticker: Any, start: date, end: date, period: str, latest: bool = False):
    from moexdata.api.corporate import dividends_between

    return dividends_between(ticker.secid, start, end, latest)


def _fetch_splits(ticker: Any, start: date, end: date, period: str, latest: bool = False):
    from moexdata.api.corporate import splits_between

    return splits_between(ticker.secid, start, end, latest)


def _fetch_futoi(ticker: Any, start: date, end: date, period: str, latest: bool = False):
    # latest для FUTOI moexalgo не поддерживает.
    return ticker.futoi(start=start, end=end)


@dataclass(frozen=True)
class DatasetInfo:
    key: str
    label: str
    description: str
    fetch: FetchFn
    markets: tuple[str, ...]
    requires_period: bool = False
    chunk_days: int = 30
    time_columns: tuple[str, ...] = ()
    default_selected: bool = False
    supports_latest: bool = True
    row_limit: int | None = ROW_LIMIT
    date_column: str = "tradedate"
    daily_coverage: bool = True
    trading_rows: bool = True


DATASETS: dict[str, DatasetInfo] = {
    "candles": DatasetInfo(
        "candles",
        "Свечи (OHLCV)",
        "Открытие/максимум/минимум/закрытие, объём и оборот за интервал.",
        _fetch_candles,
        ("shares", "futures", "currency", "index", "bonds", "options"),
        requires_period=True,
        chunk_days=1,
        time_columns=("begin", "end"),
        default_selected=True,
        date_column="begin",
    ),
    "tradestats": DatasetInfo(
        "tradestats",
        "TradeStats — метрики сделок",
        "Агрегаты по сделкам за 5-минутные интервалы: объёмы покупок/продаж, VWAP и др.",
        _fetch_tradestats,
        ("shares", "futures", "currency"),
        chunk_days=30,
        time_columns=("tradedate", "tradetime"),
    ),
    "orderstats": DatasetInfo(
        "orderstats",
        "OrderStats — метрики заявок",
        "Агрегаты по выставленным и снятым заявкам за 5-минутные интервалы.",
        _fetch_orderstats,
        ("shares", "futures", "currency"),
        chunk_days=30,
        time_columns=("tradedate", "tradetime"),
    ),
    "obstats": DatasetInfo(
        "obstats",
        "OBStats — метрики стакана",
        "Состояние книги заявок: спред, дисбаланс, объёмы на уровнях.",
        _fetch_obstats,
        ("shares", "futures", "currency"),
        chunk_days=30,
        time_columns=("tradedate", "tradetime"),
    ),
    "hi2": DatasetInfo(
        "hi2",
        "HI2 — индекс концентрации",
        "Индекс рыночной концентрации участников торгов.",
        _fetch_hi2,
        ("shares", "futures", "currency"),
        chunk_days=90,
        time_columns=("tradedate", "tradetime"),
    ),
    "alerts": DatasetInfo(
        "alerts",
        "MegaAlerts — аномальная активность",
        "Сигналы о нетипичной рыночной активности по инструменту.",
        _fetch_alerts,
        ("shares", "futures", "currency"),
        chunk_days=90,
        time_columns=("tradedate", "tradetime"),
        daily_coverage=False,
    ),
    "futoi": DatasetInfo(
        "futoi",
        "FUTOI — открытые позиции",
        "Открытые позиции физических и юридических лиц по фьючерсам.",
        _fetch_futoi,
        ("futures",),
        chunk_days=30,
        time_columns=("tradedate", "tradetime"),
        supports_latest=False,
        row_limit=None,
    ),
}


DATASETS["dividends"] = DatasetInfo(
    "dividends",
    "Дивиденды",
    "Дата закрытия реестра, размер выплаты, валюта и последний день покупки под дивиденд.",
    _fetch_dividends,
    ("shares",),
    chunk_days=0,
    time_columns=("registryclosedate",),
    supports_latest=True,
    row_limit=None,
    date_column="registryclosedate",
    daily_coverage=False,
    trading_rows=False,
)
DATASETS["splits"] = DatasetInfo(
    "splits",
    "Сплиты и консолидации",
    "Дата и соотношение бумаг до/после дробления или консолидации акций.",
    _fetch_splits,
    ("shares",),
    chunk_days=0,
    time_columns=("tradedate",),
    supports_latest=True,
    row_limit=None,
    daily_coverage=False,
    trading_rows=False,
)


def datasets_for_market(market_key: str) -> list[DatasetInfo]:
    return [ds for ds in DATASETS.values() if market_key in ds.markets]


def auto_chunk_days(dataset_key: str, period: str) -> int:
    dataset = DATASETS[dataset_key]
    if dataset.requires_period:
        if not period_is_splittable(period):
            return 0
        return max(1, period_chunk_days(period))
    if dataset.chunk_days <= 0:
        return 0
    return dataset.chunk_days
