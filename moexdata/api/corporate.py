from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from moexdata.errors import ExportError

MANUAL_DIRS: list[Path] = []
MANUAL_FILE = "dividends.csv"

# С этой даты расчёты по акциям идут в режиме T+1.
T1_SINCE = date(2023, 7, 31)


def _get_block(path: str, block: str) -> pd.DataFrame:
    from moexdata.api import iss

    try:
        raw, _ = iss.fetch(path, block=block)
    except iss.NoData:
        return pd.DataFrame()
    except Exception as exc:  # noqa: BLE001
        if _is_not_found(exc):
            return pd.DataFrame()
        raise
    return iss.table(raw, block)


def _is_not_found(exc: Exception) -> bool:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status == 404


def last_buy_date(registry_close: date) -> date:
    lag = 1 if registry_close >= T1_SINCE else 2
    day = registry_close
    while lag:
        day -= timedelta(days=1)
        if day.weekday() < 5:
            lag -= 1
    return day


# securities/{secid}/dividends сейчас отдаёт карточку бумаги без выплат,
# cci/corp-actions требует отдельной подписки (403 с ключом Algopack).
DIVIDEND_SOURCES = (
    ("securities/{secid}/dividends", {}),
    ("cci/corp-actions/dividends", {"secid": "{secid}"}),
)

_DIV_SECID = ("secid", "ticker", "security", "seccode")
_DIV_DATE = ("registryclosedate", "registry_close_date", "record_date", "recorddate", "registry_date")
_DIV_VALUE = ("value", "dividend_value", "div_value", "amount", "dividend")
_DIV_CURRENCY = ("currencyid", "currency_id", "currency")


def _pick(columns: list[str], names: tuple[str, ...], contains: tuple[str, ...] = ()) -> str | None:
    found = next((c for c in names if c in columns), None)
    if found is None and contains:
        found = next((c for c in columns if all(part in c for part in contains)), None)
    return found


def normalize_dividends(raw: dict, secid: str) -> pd.DataFrame:
    from moexdata.api import iss

    for name, block in (raw or {}).items():
        if not isinstance(block, dict) or not block.get("data"):
            continue
        df = iss.table(raw, name)
        columns = list(df.columns)
        date_col = _pick(columns, _DIV_DATE, ("registry", "date")) or _pick(columns, (), ("record", "date"))
        value_col = _pick(columns, _DIV_VALUE)
        if date_col is None or value_col is None:
            continue
        sec_col = _pick(columns, _DIV_SECID)
        cur_col = _pick(columns, _DIV_CURRENCY)
        if sec_col is not None:
            df = df[df[sec_col].astype(str).str.upper() == secid.upper()]
        result = pd.DataFrame({
            "secid": secid,
            "registryclosedate": df[date_col].values,
            "value": df[value_col].values,
            "currencyid": df[cur_col].values if cur_col else "RUB",
        })
        return result
    return pd.DataFrame()


def load_manual_dividends(secid: str) -> pd.DataFrame | None:
    for directory in MANUAL_DIRS:
        path = Path(directory) / MANUAL_FILE
        if not path.is_file():
            continue
        raw = pd.read_csv(path, sep=None, engine="python", encoding="utf-8-sig", dtype=str)
        raw.columns = [str(c).strip().lower() for c in raw.columns]
        df = normalize_dividends(
            {"manual": {"columns": list(raw.columns), "data": raw.values.tolist()}}, secid
        )
        if not df.empty:
            df["value"] = pd.to_numeric(df["value"].astype(str).str.replace(",", "."), errors="coerce")
        return df
    return None


def _looks_like_dividends(raw: dict) -> bool:
    for block in (raw or {}).values():
        columns = [str(c).lower() for c in (block or {}).get("columns", [])] if isinstance(block, dict) else []
        if _pick(columns, _DIV_DATE, ("registry", "date")) and _pick(columns, _DIV_VALUE):
            return True
    return False


def fetch_dividends(secid: str) -> pd.DataFrame:
    from moexdata.api import iss

    manual = load_manual_dividends(secid)
    df = manual if manual is not None else pd.DataFrame()
    answered = manual is not None
    forbidden = False
    if manual is None:
        for path, params in DIVIDEND_SOURCES:
            try:
                raw, _ = iss.fetch(
                    path.format(secid=secid), {k: v.format(secid=secid) for k, v in params.items()}
                )
            except iss.NoData:
                continue
            except Exception as exc:  # noqa: BLE001
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status in (401, 403):
                    forbidden = True
                    continue
                if _is_not_found(exc):
                    continue
                raise
            df = normalize_dividends(raw, secid)
            if not df.empty or _looks_like_dividends(raw):
                answered = True
                break
    if df.empty and not answered:
        reason = "раздел корпоративных действий MOEX требует отдельной подписки (403)" if forbidden             else "ISS не отдал таблицу дивидендов"
        raise ExportError(
            "unsupported",
            f"Дивиденды недоступны: {reason}.",
            False,
            f"Положите свой файл {MANUAL_FILE} (secid;registryclosedate;value) в папку выгрузки.",
        )
    if df.empty:
        return pd.DataFrame()
    df["registryclosedate"] = pd.to_datetime(df["registryclosedate"], errors="coerce")
    df = df.dropna(subset=["registryclosedate"])
    df["value"] = pd.to_numeric(df.get("value"), errors="coerce")
    df["last_buy_date"] = df["registryclosedate"].map(lambda d: pd.Timestamp(last_buy_date(d.date())))
    return df.sort_values("registryclosedate").reset_index(drop=True)


def fetch_splits(secid: str) -> pd.DataFrame:
    df = _get_block(f"statistics/engines/stock/splits/{secid}", "splits")
    if df.empty:
        everything = _get_block("statistics/engines/stock/splits", "splits")
        if not everything.empty and "secid" in everything.columns:
            df = everything[everything["secid"].astype(str).str.upper() == secid.upper()].copy()
    if df.empty or "tradedate" not in df.columns:
        return pd.DataFrame()
    df["tradedate"] = pd.to_datetime(df["tradedate"], errors="coerce")
    for column in ("before", "after"):
        df[column] = pd.to_numeric(df.get(column), errors="coerce")
    df = df.dropna(subset=["tradedate", "before", "after"])
    return df.sort_values("tradedate").reset_index(drop=True)


def _in_range(df: pd.DataFrame, column: str, start: date, end: date, latest: bool) -> pd.DataFrame:
    if df.empty:
        return df
    days = df[column].dt.date
    df = df[(days >= start) & (days <= end)].copy()
    df[column] = df[column].dt.strftime("%Y-%m-%d")
    if "last_buy_date" in df.columns:
        df["last_buy_date"] = df["last_buy_date"].dt.strftime("%Y-%m-%d")
    return df.tail(1) if latest else df


def dividends_between(secid: str, start: date, end: date, latest: bool = False) -> pd.DataFrame:
    return _in_range(fetch_dividends(secid), "registryclosedate", start, end, latest)


def splits_between(secid: str, start: date, end: date, latest: bool = False) -> pd.DataFrame:
    return _in_range(fetch_splits(secid), "tradedate", start, end, latest)
