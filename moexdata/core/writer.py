from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

INVALID_CHARS = re.compile(r'[<>:"/\|?*\x00-\x1f]')


def safe_name(value: str) -> str:
    cleaned = INVALID_CHARS.sub("_", str(value)).strip(" .")
    return cleaned or "file"


def build_filename(
    template: str,
    *,
    ticker: str,
    dataset: str,
    period: str,
    start: str,
    end: str,
    market: str,
) -> str:
    period_part = f"_{period}" if period else ""
    try:
        name = template.format(
            ticker=ticker,
            dataset=dataset,
            period=period_part,
            period_raw=period,
            start=start,
            end=end,
            market=market,
        )
    except (KeyError, IndexError, ValueError):
        name = f"{ticker}_{dataset}{period_part}_{start}_{end}"
    return safe_name(name)


def target_path(directory: Path, filename: str, file_format: str, csv_gzip: bool) -> Path:
    if file_format == "parquet":
        return directory / f"{filename}.parquet"
    return directory / (f"{filename}.csv.gz" if csv_gzip else f"{filename}.csv")


def write_dataframe(
    df: pd.DataFrame,
    path: Path,
    file_format: str,
    *,
    csv_sep: str = ",",
    csv_decimal: str = ".",
    csv_encoding: str = "utf-8-sig",
    csv_gzip: bool = False,
    parquet_compression: str = "snappy",
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if file_format == "parquet":
        compression = None if parquet_compression == "none" else parquet_compression
        df.to_parquet(path, index=False, compression=compression)
    else:
        df.to_csv(
            path,
            index=False,
            sep=csv_sep,
            decimal=csv_decimal,
            encoding=csv_encoding,
            compression="gzip" if csv_gzip else None,
        )
    return path


def ensure_parquet_available() -> None:
    try:
        import pyarrow  # noqa: F401
    except ImportError:
        try:
            import fastparquet  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "pyarrow (или fastparquet) — требуется для формата Parquet"
            ) from exc
