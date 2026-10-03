from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from datetime import timedelta
from pathlib import Path

from moexdata.timeutil import moscow_today

APP_NAME = "MoexData"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EXPORT_DIR = PROJECT_ROOT / "data"

SETTINGS_VERSION = 2


def config_dir() -> Path:
    base = os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / ".config" / APP_NAME


CONFIG_PATH = config_dir() / "settings.json"


def _default_start() -> str:
    return (moscow_today() - timedelta(days=30)).isoformat()


def _default_end() -> str:
    return moscow_today().isoformat()


@dataclass
class Settings:
    settings_version: int = SETTINGS_VERSION

    api_key: str = ""

    market: str = "shares"
    tickers: list[str] = field(default_factory=list)
    data_types: list[str] = field(default_factory=lambda: ["candles"])
    candle_period: str = "1min"
    start_date: str = field(default_factory=_default_start)
    end_date: str = field(default_factory=_default_end)
    full_history: bool = False

    out_dir: str = str(DEFAULT_EXPORT_DIR)
    file_format: str = "parquet"  # csv | parquet
    filename_template: str = "{ticker}_{dataset}{period}_{start}_{end}"
    subfolder_per_ticker: bool = True
    combine_tickers: bool = False
    write_manifest: bool = True

    chunk_mode: str = "auto"  # auto | manual
    chunk_days: int = 1
    skip_weekends: bool = False

    retries: int = 3
    retry_pause: float = 3.0
    request_pause: float = 0.0
    use_cache: bool = True
    keep_parts: bool = False

    dedupe: bool = True
    sort_rows: bool = True
    add_ticker_column: bool = True
    parse_dates: bool = True
    use_calendar: bool = True
    validate_data: bool = True
    adjust_prices: bool = False
    update_existing: bool = False

    continuous: bool = False
    roll_days: int = 5
    roll_adjust: str = "diff"  # none | diff | ratio
    quarterly_only: bool = False

    csv_sep: str = ","
    csv_decimal: str = "."
    csv_encoding: str = "utf-8-sig"
    csv_gzip: bool = False
    parquet_compression: str = "snappy"

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or CONFIG_PATH
        settings = cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return settings
        if not isinstance(raw, dict):
            return settings
        known = {f.name: f.type for f in fields(cls)}
        for key, value in raw.items():
            if key in known:
                setattr(settings, key, value)
        # До версии 2 выходные пропускались по умолчанию и терялись выходные сессии.
        version = raw.get("settings_version", 1)
        if not isinstance(version, int) or version < 2:
            settings.skip_weekends = False
        settings.settings_version = SETTINGS_VERSION
        settings.normalize()
        return settings

    def save(self, path: Path | None = None) -> Path:
        path = path or CONFIG_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    def normalize(self) -> None:
        if self.file_format not in ("csv", "parquet"):
            self.file_format = "parquet"
        if self.chunk_mode not in ("auto", "manual"):
            self.chunk_mode = "auto"
        self.chunk_days = max(1, int(self.chunk_days or 1))
        self.retries = max(0, int(self.retries or 0))
        self.retry_pause = max(0.0, float(self.retry_pause or 0))
        self.request_pause = max(0.0, float(self.request_pause or 0))
        self.roll_days = max(0, int(self.roll_days or 0))
        if self.roll_adjust not in ("none", "diff", "ratio"):
            self.roll_adjust = "diff"
        if self.parquet_compression not in ("snappy", "gzip", "brotli", "zstd", "none"):
            self.parquet_compression = "snappy"
        if not isinstance(self.tickers, list):
            self.tickers = []
        if not isinstance(self.data_types, list) or not self.data_types:
            self.data_types = ["candles"]

    def resolved_api_key(self) -> str:
        return (
            self.api_key.strip()
            or os.environ.get("MOEXALGO_APIKEY", "").strip()
            or os.environ.get("ALGOPACK_APIKEY", "").strip()
        )
