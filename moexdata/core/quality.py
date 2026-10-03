from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

LEVELS = ("error", "warn", "info")

JUMP_THRESHOLD = 0.05
CROSS_MATCH_MIN = 0.95
MAX_EXAMPLES = 5


@dataclass
class Issue:
    check: str
    level: str
    count: int
    message: str
    examples: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _examples(df: pd.DataFrame, mask: pd.Series, column: str | None) -> list[str]:
    rows = df.loc[mask]
    if column is None or column not in rows.columns:
        return [str(i) for i in rows.index[:MAX_EXAMPLES]]
    head = rows.head(MAX_EXAMPLES)
    if column == "tradedate" and "tradetime" in head.columns:
        day = pd.to_datetime(head["tradedate"], errors="coerce").dt.strftime("%Y-%m-%d")
        return [f"{d} {t}" for d, t in zip(day, head["tradetime"])]
    return [str(v) for v in head[column]]


def _timestamp(df: pd.DataFrame) -> pd.Series | None:
    if "begin" in df.columns:
        return pd.to_datetime(df["begin"], errors="coerce")
    if "tradedate" in df.columns and "tradetime" in df.columns:
        day = pd.to_datetime(df["tradedate"], errors="coerce").dt.strftime("%Y-%m-%d")
        return pd.to_datetime(day + " " + df["tradetime"].astype(str), errors="coerce")
    return None


def _label_column(df: pd.DataFrame) -> str | None:
    for column in ("begin", "tradedate", "registryclosedate"):
        if column in df.columns:
            return column
    return None


def _ohlc(
    df: pd.DataFrame, o: str, h: str, low: str, c: str, label: str | None, issues: list[Issue]
) -> None:
    if not {o, h, low, c} <= set(df.columns):
        return
    prices = df[[o, h, low, c]].apply(pd.to_numeric, errors="coerce")
    bad = (prices[h] < prices[[o, c, low]].max(axis=1)) | (prices[low] > prices[[o, c, h]].min(axis=1))
    if bad.any():
        issues.append(Issue(
            "ohlc", "error", int(bad.sum()),
            "максимум ниже открытия/закрытия или минимум выше них", _examples(df, bad, label),
        ))
    nonpos = (prices <= 0).any(axis=1)
    if nonpos.any():
        issues.append(Issue(
            "nonpositive_price", "error", int(nonpos.sum()), "нулевые или отрицательные цены",
            _examples(df, nonpos, label),
        ))


def _negative(df: pd.DataFrame, columns: tuple[str, ...], label: str | None, issues: list[Issue]) -> None:
    present = [c for c in columns if c in df.columns]
    if not present:
        return
    values = df[present].apply(pd.to_numeric, errors="coerce")
    bad = (values < 0).any(axis=1)
    if bad.any():
        issues.append(Issue(
            "negative_volume", "error", int(bad.sum()), f"отрицательные значения в {', '.join(present)}",
            _examples(df, bad, label),
        ))


def _time_order(df: pd.DataFrame, keys: list[str], label: str | None, issues: list[Issue]) -> None:
    stamp = _timestamp(df)
    if stamp is None:
        return
    key = pd.DataFrame({"_t": stamp})
    for column in keys:
        if column in df.columns:
            key[column] = df[column].astype(str)
    dup = key.duplicated(keep=False) & stamp.notna()
    if dup.any():
        issues.append(Issue(
            "duplicate_time", "error", int(dup.sum()), "повторяющиеся метки времени",
            _examples(df, dup, label),
        ))
    if not stamp.dropna().is_monotonic_increasing:
        issues.append(Issue("unsorted", "warn", 1, "строки не отсортированы по времени"))


def _nulls(df: pd.DataFrame, issues: list[Issue], ignore: tuple[str, ...] = ()) -> None:
    counts = {
        c: int(n) for c, n in df.isna().sum().items()
        if n and c not in ignore and not str(c).startswith("adj_")
    }
    if counts:
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:MAX_EXAMPLES]
        issues.append(Issue(
            "nulls", "info", sum(counts.values()), "пропуски значений по колонкам",
            [f"{c}: {n}" for c, n in top],
        ))


def _jumps(df: pd.DataFrame, close: str, label: str | None, issues: list[Issue]) -> None:
    if close not in df.columns or len(df) < 2:
        return
    prices = pd.to_numeric(df[close], errors="coerce")
    change = prices.pct_change()
    # Смена контракта в склейке - шов, а не скачок.
    if "contract" in df.columns:
        change = change.where(df["contract"].eq(df["contract"].shift()))
    jump = change.abs() > JUMP_THRESHOLD
    if jump.any():
        examples = [
            f"{stamp}: {change[i] * 100:+.1f}%"
            for i, stamp in zip(df.index[jump][:MAX_EXAMPLES], _examples(df, jump, label))
        ]
        issues.append(Issue(
            "price_jump", "warn", int(jump.sum()),
            f"скачок цены больше {JUMP_THRESHOLD:.0%} между соседними строками "
            "(бывает при дивидендном гэпе или сплите)",
            examples,
        ))


def check_frame(df: pd.DataFrame, dataset_key: str, market: str = "shares") -> list[Issue]:
    issues: list[Issue] = []
    if df is None or df.empty:
        return issues
    label = _label_column(df)

    if dataset_key == "candles":
        _ohlc(df, "open", "high", "low", "close", label, issues)
        _negative(df, ("volume", "value"), label, issues)
        _time_order(df, ["contract"] if "contract" in df.columns else [], label, issues)
        _jumps(df, "close", label, issues)
        # У фьючерсов оборот в рублях, а цена в пунктах, поэтому проверка только для акций.
        if market == "shares" and {"value", "volume", "low", "high"} <= set(df.columns):
            volume = pd.to_numeric(df["volume"], errors="coerce").replace(0, np.nan)
            vwap = pd.to_numeric(df["value"], errors="coerce") / volume
            off = (vwap < df["low"] * 0.999) | (vwap > df["high"] * 1.001)
            if off.any():
                issues.append(Issue(
                    "vwap_range", "warn", int(off.sum()),
                    "оборот/объём вне диапазона min—max бара (возможна иная единица объёма)",
                    _examples(df, off, label),
                ))
        _nulls(df, issues)
    elif dataset_key == "tradestats":
        _ohlc(df, "pr_open", "pr_high", "pr_low", "pr_close", label, issues)
        _negative(df, ("vol", "val", "trades", "vol_b", "vol_s"), label, issues)
        _time_order(df, [], label, issues)
        _jumps(df, "pr_close", label, issues)
        _nulls(df, issues)
    elif dataset_key in ("orderstats", "obstats"):
        _negative(df, ("put_vol", "cancel_vol", "vol_b", "vol_s"), label, issues)
        _time_order(df, [], label, issues)
        _nulls(df, issues)
    elif dataset_key in ("hi2", "alerts", "futoi"):
        keys = {"hi2": ["metric"], "alerts": ["alert_type"], "futoi": ["clgroup"]}[dataset_key]
        _time_order(df, keys, label, issues)
        _nulls(df, issues, ignore=("reference",))
    elif dataset_key == "dividends":
        if "value" in df.columns:
            bad = pd.to_numeric(df["value"], errors="coerce") <= 0
            if bad.any():
                issues.append(Issue(
                    "nonpositive_dividend", "warn", int(bad.sum()), "дивиденд не больше нуля",
                    _examples(df, bad, label),
                ))
    return issues


def cross_check_candles_tradestats(candles: pd.DataFrame, stats: pd.DataFrame) -> list[Issue]:
    need_c = {"begin", "high", "low", "volume"}
    need_s = {"tradedate", "tradetime", "pr_high", "pr_low", "vol"}
    if candles.empty or stats.empty or not need_c <= set(candles.columns) or not need_s <= set(stats.columns):
        return []
    begin = pd.to_datetime(candles["begin"], errors="coerce")
    # TradeStats помечает интервал временем конца: 07:05 - это минутки 07:00-07:04.
    bucket = begin.dt.floor("5min") + pd.Timedelta(minutes=5)
    agg = (
        candles.assign(_k=bucket)
        .groupby("_k")
        .agg(h=("high", "max"), l=("low", "min"), v=("volume", "sum"))
    )
    stamp = _timestamp(stats)
    joined = stats.assign(_k=stamp).set_index("_k").join(agg, how="inner")
    if joined.empty:
        return [Issue("cross_check", "warn", 0, "свечи и TradeStats не пересекаются по времени")]
    match = {
        "max": np.isclose(joined["pr_high"], joined["h"]).mean(),
        "min": np.isclose(joined["pr_low"], joined["l"]).mean(),
        "объём": np.isclose(joined["vol"], joined["v"]).mean(),
    }
    worst = min(match.values())
    level = "info" if worst >= CROSS_MATCH_MIN else "warn"
    text = ", ".join(f"{name} {share:.1%}" for name, share in match.items())
    return [Issue(
        "cross_check", level, int(len(joined)),
        f"сверка 5-минуток из свечей с TradeStats на {len(joined)} интервалах: {text}",
    )]


def describe(issues: list[Issue]) -> str:
    serious = [i for i in issues if i.level != "info"]
    if not serious:
        return "проверка: замечаний нет"
    errors = sum(1 for i in serious if i.level == "error")
    return f"проверка: замечаний {len(serious)}" + (f", из них ошибок {errors}" if errors else "")
