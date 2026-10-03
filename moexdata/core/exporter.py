from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable

import pandas as pd

from moexdata.api import client, corporate
from moexdata.api.datasets import DATASETS, MARKETS, ROW_LIMIT, DatasetInfo, auto_chunk_days
from moexdata.core import writer
from moexdata.core.adjust import RollGap, apply_factors, apply_roll_gaps, corporate_events
from moexdata.core.chunking import drop_non_trading, split_range
from moexdata.core.continuous import Contract, build_schedule, output_name, select_contracts
from moexdata.core.quality import check_frame, cross_check_candles_tradestats
from moexdata.core.quality import describe as describe_quality
from moexdata.errors import ConfigError, ExportError, classify
from moexdata.timeutil import is_settled, moscow_today

ProgressCallback = Callable[[int, int, str], None]
LogCallback = Callable[[str, str], None]
StopCheck = Callable[[], bool]

CACHE_DIRNAME = ".moexdata_cache"


@dataclass
class ExportJob:
    api_key: str
    market: str
    tickers: list[str]
    datasets: list[str]
    start: date
    end: date
    out_dir: Path
    candle_period: str = "1min"
    file_format: str = "parquet"

    full_history: bool = False

    chunk_mode: str = "auto"
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

    combine_tickers: bool = False
    subfolder_per_ticker: bool = True
    write_manifest: bool = True

    filename_template: str = "{ticker}_{dataset}{period}_{start}_{end}"
    csv_sep: str = ","
    csv_decimal: str = "."
    csv_encoding: str = "utf-8-sig"
    csv_gzip: bool = False
    parquet_compression: str = "snappy"

    only_pairs: set[tuple[str, str]] | None = None

    def validate(self) -> None:
        if not self.api_key.strip():
            raise ConfigError("Не задан API-ключ Algopack — заполните поле вверху окна.")
        if self.market not in MARKETS:
            raise ConfigError(f"Неизвестный рынок: {self.market}")
        if not self.tickers:
            raise ConfigError("Не выбран ни один тикер.")
        if not self.datasets:
            raise ConfigError("Не выбран ни один тип данных.")
        unknown = [key for key in self.datasets if key not in DATASETS]
        if unknown:
            raise ConfigError("Неизвестные типы данных: " + ", ".join(unknown))
        wrong = [DATASETS[k].label for k in self.datasets if self.market not in DATASETS[k].markets]
        if wrong:
            raise ConfigError(
                f"Недоступно для рынка «{MARKETS[self.market].label}»: " + ", ".join(wrong)
            )
        if not self.full_history:
            if self.start > self.end:
                raise ConfigError("Дата начала не может быть позже даты окончания.")
            if self.end > moscow_today():
                raise ConfigError("Дата окончания не может быть в будущем.")
        if self.file_format not in ("csv", "parquet"):
            raise ConfigError(f"Неподдерживаемый формат файла: {self.file_format}")
        if self.file_format == "parquet":
            writer.ensure_parquet_available()
        if self.chunk_mode == "manual" and self.chunk_days < 1:
            raise ConfigError("Размер части выгрузки должен быть не меньше 1 дня.")
        if self.continuous and self.market != "futures":
            raise ConfigError("Склейка контрактов доступна только для фьючерсов.")
        if self.roll_adjust not in ("none", "diff", "ratio"):
            raise ConfigError(f"Неизвестный способ корректировки швов: {self.roll_adjust}")
        if self.update_existing and self.combine_tickers:
            raise ConfigError(
                "Догрузка работает с файлами по отдельным тикерам — выключите «один общий файл»."
            )

    def chunk_days_for(self, dataset_key: str) -> int:
        dataset = DATASETS[dataset_key]
        if not dataset.requires_period and dataset.chunk_days <= 0:
            return 0
        if self.chunk_mode == "manual":
            return max(1, self.chunk_days)
        return auto_chunk_days(dataset_key, self.candle_period)

    def pairs(self) -> list[tuple[str, str]]:
        result = [(t, d) for t in self.tickers for d in self.datasets]
        if self.only_pairs is not None:
            result = [pair for pair in result if pair in self.only_pairs]
        return result


@dataclass
class ExportResult:
    ticker: str
    dataset: str
    status: str  # ok | empty | error
    rows: int = 0
    chunks_ok: int = 0
    chunks_failed: int = 0
    path: str | None = None
    code: str = ""
    message: str = ""
    start: str = ""
    end: str = ""


@dataclass
class ExportSummary:
    started_at: str = ""
    finished_at: str = ""
    job: dict = field(default_factory=dict)
    results: list[ExportResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    quality: list[dict] = field(default_factory=list)
    calendar: dict = field(default_factory=dict)
    stopped: bool = False

    @property
    def ok(self) -> int:
        return sum(1 for r in self.results if r.status == "ok")

    @property
    def empty(self) -> int:
        return sum(1 for r in self.results if r.status == "empty")

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == "error")

    @property
    def total_rows(self) -> int:
        return sum(r.rows for r in self.results)

    def failed_pairs(self) -> set[tuple[str, str]]:
        return {(r.ticker, r.dataset) for r in self.results if r.status == "error"}

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "stopped": self.stopped,
            "job": self.job,
            "totals": {
                "ok": self.ok,
                "empty": self.empty,
                "failed": self.failed,
                "rows": self.total_rows,
            },
            "warnings": self.warnings,
            "quality": self.quality,
            "calendar": self.calendar,
            "results": [r.__dict__ for r in self.results],
        }


class ChunkCache:
    def __init__(self, root: Path, enabled: bool):
        self.root = root
        self.enabled = enabled
        self._format = "parquet"
        if enabled:
            try:
                writer.ensure_parquet_available()
            except ImportError:
                self._format = "csv"

    def path_for(
        self, market: str, ticker: str, dataset: str, period: str, start: date, end: date
    ) -> Path:
        period_part = f"_{period}" if DATASETS[dataset].requires_period else ""
        name = writer.safe_name(f"{market}_{ticker}_{dataset}{period_part}_{start}_{end}")
        return self.root / writer.safe_name(ticker) / dataset / f"{name}.{self._format}"

    def load(self, path: Path) -> pd.DataFrame | None:
        if not self.enabled:
            return None
        if path.with_suffix(".empty").exists():
            return pd.DataFrame()
        if not path.exists():
            return None
        try:
            if self._format == "parquet":
                return pd.read_parquet(path)
            return pd.read_csv(path)
        except Exception:  # noqa: BLE001
            try:
                path.unlink()
            except OSError:
                pass
            return None

    def store(self, path: Path, df: pd.DataFrame, final: bool = True) -> None:
        # Незакрытый день не кешируем, иначе следующий запуск поднимет неполные данные.
        if not self.enabled or not final:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if df.empty:
                path.with_suffix(".empty").touch()
                return
            if self._format == "parquet":
                df.to_parquet(path, index=False)
            else:
                df.to_csv(path, index=False, encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass


def _concat(frames: Iterable[pd.DataFrame]) -> pd.DataFrame:
    frames = [f for f in frames if f is not None and not f.empty]
    if not frames:
        return pd.DataFrame()
    if len(frames) == 1:
        return frames[0].copy()
    return pd.concat(frames, ignore_index=True, sort=False)


def _hashable_columns(df: pd.DataFrame) -> list[str]:
    result = []
    for column in df.columns:
        sample = df[column].head(50)
        if any(isinstance(value, (list, dict, set)) for value in sample):
            continue
        result.append(column)
    return result


DATETIME_COLUMNS = ("begin", "end", "tradedate", "systime")


def _blanks_to_na(df: pd.DataFrame) -> pd.DataFrame:
    for column in _hashable_columns(df):
        series = df[column]
        if pd.api.types.is_string_dtype(series) or series.dtype == object:
            blank = series.map(lambda value: isinstance(value, str) and value == "")
            if blank.any():
                df[column] = series.mask(blank)
    return df


def _parse_datetimes(df: pd.DataFrame) -> pd.DataFrame:
    for column in DATETIME_COLUMNS:
        if column not in df.columns or pd.api.types.is_datetime64_any_dtype(df[column]):
            continue
        parsed = pd.to_datetime(df[column], errors="coerce", format="ISO8601")
        if parsed.notna().sum() == df[column].notna().sum():
            df[column] = parsed
    return df


def _drop_weekends(df: pd.DataFrame, dataset: DatasetInfo) -> pd.DataFrame:
    if not dataset.trading_rows or dataset.date_column not in df.columns:
        return df
    days = pd.to_datetime(df[dataset.date_column], errors="coerce")
    return df[~(days.dt.weekday >= 5)]


def postprocess(df: pd.DataFrame, dataset_key: str, ticker: str, job: ExportJob) -> pd.DataFrame:
    if df.empty:
        return df
    dataset = DATASETS[dataset_key]
    df = _blanks_to_na(df.copy())

    if job.dedupe:
        # Дубли ищем по всей строке: у FUTOI на одну метку времени несколько строк.
        subset = _hashable_columns(df) or None
        try:
            df = df.drop_duplicates(subset=subset, keep="first")
        except TypeError:
            pass

    if job.skip_weekends:
        df = _drop_weekends(df, dataset)
    if job.parse_dates:
        df = _parse_datetimes(df)

    if job.sort_rows:
        sort_cols = [c for c in dataset.time_columns if c in df.columns]
        if sort_cols:
            try:
                df = df.sort_values(sort_cols, kind="stable")
            except TypeError:
                pass

    df = df.reset_index(drop=True)

    if job.add_ticker_column and "ticker" not in df.columns and "secid" not in df.columns:
        df.insert(0, "ticker", ticker)
    elif "ticker" in df.columns and df["ticker"].isna().any():
        df["ticker"] = df["ticker"].fillna(ticker)
    return df


@dataclass
class _Existing:
    path: Path
    frame: pd.DataFrame
    start: date


@dataclass
class _Plan:
    ticker: str
    dataset_key: str
    name: str
    start: date
    end: date
    chunks: list[tuple[date, date, str]]
    existing: _Existing | None = None


def run_export(
    job: ExportJob,
    progress_cb: ProgressCallback | None = None,
    log_cb: LogCallback | None = None,
    should_stop: StopCheck | None = None,
) -> ExportSummary:
    log = log_cb or (lambda level, message: None)
    progress = progress_cb or (lambda done, total, message: None)
    stop = should_stop or (lambda: False)

    job.validate()
    client.configure(job.api_key)
    corporate.MANUAL_DIRS[:] = [job.out_dir]

    summary = ExportSummary(started_at=datetime.now().isoformat(timespec="seconds"))
    summary.job = _job_to_dict(job)

    plans: list[_Plan] = []
    plan_fatal: tuple[tuple[str, str], ExportError] | None = None
    series: dict[str, tuple[str, list[Contract]] | ExportError] = {}

    if job.full_history:
        log("info", "Определяю доступную глубину истории по каждому инструменту…")
    if job.update_existing:
        log("info", "Догрузка: ищу ранее выгруженные файлы…")

    for ticker, dataset_key in job.pairs():
        if stop():
            summary.stopped = True
            break
        try:
            plan = _plan_pair(job, ticker, dataset_key, series, log, summary, stop)
        except ExportError as err:
            if err.code in ("auth", "forbidden", "no_api_key"):
                plan_fatal = ((ticker, dataset_key), err)
                break
            summary.results.append(ExportResult(ticker, dataset_key, "error", code=err.code, message=str(err)))
            log("error", f"✖ {ticker} / {DATASETS[dataset_key].label}: {err}")
            continue
        if plan is not None:
            plans.append(plan)

    if plan_fatal is not None:
        (ticker, dataset_key), err = plan_fatal
        summary.results.append(
            ExportResult(ticker, dataset_key, "error", code=err.code, message=str(err))
        )
        log("error", f"✖ Подготовка выгрузки прервана — {err}")
        plans = []

    calendar = None
    if job.use_calendar and plans:
        calendar = _load_calendar(job, [(p.start, p.end) for p in plans], log, summary)
    if calendar is not None:
        before = sum(len(p.chunks) for p in plans)
        for plan in plans:
            plan.chunks = _filter_chunks(plan.chunks, calendar.is_trading)
        saved = before - sum(len(p.chunks) for p in plans)
        if saved:
            log("info", f"Торговый календарь: не запрашиваются неторговые дни — минус {saved} запрос(ов)")

    open_ends = [p.end for p in plans if not is_settled(p.end)]
    if open_ends:
        warning = (
            f"Период захватывает незакрытый торговый день {max(open_ends)}: торги или расчёт "
            "метрик ещё идут, данные за него неполные. Этот день не кешируется и при "
            "следующей выгрузке скачается заново."
        )
        summary.warnings.append(warning)
        log("warn", warning)

    total_steps = sum(len(p.chunks) for p in plans) or 1
    done_steps = 0

    cache = ChunkCache(job.out_dir / CACHE_DIRNAME, job.use_cache)
    combined: dict[str, list[pd.DataFrame]] = {}
    combined_spans: dict[str, list[tuple[date, date]]] = {}
    for_cross: dict[str, dict[str, pd.DataFrame]] = {}

    log("info", f"Всего запросов к API: {total_steps}; пар «тикер × набор»: {len(plans)}")

    for plan in plans:
        if stop():
            summary.stopped = True
            break
        ticker, dataset_key = plan.ticker, plan.dataset_key
        dataset = DATASETS[dataset_key]
        span_start, span_end, chunks = plan.start, plan.end, plan.chunks
        label = f"{plan.name} / {dataset.label}"
        period = job.candle_period if dataset.requires_period else ""
        log("info", f"▶ {label}: {len(chunks)} запрос(ов) за {span_start} — {span_end}")

        frames: list[pd.DataFrame] = []
        chunks_ok = 0
        chunks_failed = 0
        last_error: ExportError | None = None
        fatal: ExportError | None = None

        for chunk_start, chunk_end, secid in chunks:
            if stop():
                summary.stopped = True
                break
            progress(done_steps, total_steps, f"{label}: {chunk_start} — {chunk_end}")
            where = f"{secid} " if job.continuous else ""

            cache_path = cache.path_for(job.market, secid, dataset_key, period, chunk_start, chunk_end)
            frame = cache.load(cache_path)
            if frame is not None:
                if not frame.empty:
                    frames.append(_tag_contract(frame, secid, job))
                chunks_ok += 1
                done_steps += 1
                log("debug", f"  из кеша {where}{chunk_start} — {chunk_end}: {len(frame)} строк")
                continue

            try:
                frame = _fetch_with_retries(
                    job, secid, dataset_key, chunk_start, chunk_end, period, log, stop
                )
            except ExportError as err:
                chunks_failed += 1
                done_steps += 1
                last_error = err
                log("error", f"  [{where}{chunk_start} — {chunk_end}] {err}")
                if err.code in ("auth", "forbidden", "no_api_key"):
                    fatal = err
                    break
                continue

            if frame is None:
                summary.stopped = True
                break

            chunks_ok += 1
            done_steps += 1
            cache.store(cache_path, frame, final=is_settled(chunk_end))
            if not frame.empty:
                if dataset.row_limit and len(frame) >= dataset.row_limit:
                    warning = (
                        f"{label} [{where}{chunk_start} — {chunk_end}]: получено {len(frame)} строк, "
                        f"достигнут лимит API ({dataset.row_limit}); данные могли обрезаться. "
                        "Уменьшите размер части выгрузки."
                    )
                    summary.warnings.append(warning)
                    log("warn", "  " + warning)
                if job.keep_parts:
                    _write_part(job, secid, dataset_key, period, chunk_start, chunk_end, frame)
                frames.append(_tag_contract(frame, secid, job))
            log("debug", f"  [{where}{chunk_start} — {chunk_end}] строк: {len(frame)}")
            if job.request_pause:
                time.sleep(job.request_pause)

        progress(done_steps, total_steps, label)

        if fatal is not None:
            summary.results.append(
                ExportResult(
                    ticker, dataset_key, "error", 0, chunks_ok, chunks_failed, None,
                    fatal.code, str(fatal), span_start.isoformat(), span_end.isoformat(),
                )
            )
            log("error", f"✖ {label}: выгрузка прервана — {fatal}")
            break

        fresh = _concat(frames)
        if plan.existing is not None:
            if chunks_failed and fresh.empty:
                summary.results.append(
                    ExportResult(
                        ticker, dataset_key, "error", 0, chunks_ok, chunks_failed, None,
                        last_error.code if last_error else "", str(last_error or ""),
                        span_start.isoformat(), span_end.isoformat(),
                    )
                )
                log("error", f"  {label}: догрузка не удалась, файл оставлен без изменений")
                continue
            new_rows = len(fresh)
            fresh = _concat([plan.existing.frame, fresh])
            span_start = plan.existing.start
            log("info", f"  {label}: к файлу {plan.existing.path.name} догружено строк: {new_rows}")

        merged = postprocess(fresh, dataset_key, plan.name, job)
        notes: list[str] = []
        if calendar is not None and not merged.empty and chunks_failed == 0 and not summary.stopped:
            gap_note = _check_gaps(merged, dataset, calendar, job, label, log, summary)
            if gap_note:
                notes.append(gap_note)

        if merged.empty:
            unavailable = last_error is not None and last_error.code == "unsupported"
            status = "error" if chunks_failed and not chunks_ok and not unavailable else "empty"
            if unavailable:
                # Источника нет (например, дивиденды без подписки) - повтор не поможет.
                message, code = str(last_error), last_error.code
            elif status == "error":
                message, code = str(last_error), last_error.code if last_error else ""
            elif not dataset.trading_rows:
                message, code = "Событий за период не было", ""
            else:
                message, code = "Нет данных за выбранный период", ""
            summary.results.append(
                ExportResult(
                    ticker, dataset_key, status, 0, chunks_ok, chunks_failed, None, code, message,
                    span_start.isoformat(), span_end.isoformat(),
                )
            )
            log("warn" if status == "empty" else "error", f"  {label}: {message}")
            continue

        if dataset_key == "candles":
            merged = _adjust_candles(job, plan, merged, label, log, summary, stop)

        if job.validate_data:
            issues = check_frame(merged, dataset_key, job.market)
            _report_quality(summary, plan.name, dataset_key, issues, label, log)
            notes.append(describe_quality(issues))
            if dataset_key in ("candles", "tradestats"):
                for_cross.setdefault(plan.name, {})[dataset_key] = merged

        if job.combine_tickers:
            combined.setdefault(dataset_key, []).append(merged)
            combined_spans.setdefault(dataset_key, []).append((span_start, span_end))
            summary.results.append(
                ExportResult(
                    ticker, dataset_key, "ok", len(merged), chunks_ok, chunks_failed,
                    None, "", "; ".join(["Добавлено в общий файл", *notes]),
                    span_start.isoformat(), span_end.isoformat(),
                )
            )
            log("info", f"  {label}: {len(merged)} строк (в общий файл)")
            continue

        try:
            out_path = _write_result(job, plan.name, dataset_key, period, merged, span_start, span_end)
        except Exception as exc:  # noqa: BLE001
            err = classify(exc)
            summary.results.append(
                ExportResult(
                    ticker, dataset_key, "error", len(merged), chunks_ok, chunks_failed,
                    None, err.code, str(err), span_start.isoformat(), span_end.isoformat(),
                )
            )
            log("error", f"  {label}: {err}")
            continue

        if plan.existing is not None and plan.existing.path.resolve() != out_path.resolve():
            try:
                plan.existing.path.unlink()
            except OSError as exc:
                log("warn", f"  {label}: не удалось удалить прежний файл {plan.existing.path.name}: {exc}")

        if chunks_failed:
            notes.insert(0, f"частей с ошибкой: {chunks_failed}")
        summary.results.append(
            ExportResult(
                ticker, dataset_key, "ok", len(merged), chunks_ok, chunks_failed, str(out_path), "",
                "; ".join(n for n in notes if n), span_start.isoformat(), span_end.isoformat(),
            )
        )
        log("ok", f"✔ {label}: {len(merged)} строк → {out_path}")

    if job.validate_data and job.candle_period == "1min":
        for name, frames_by_ds in for_cross.items():
            if {"candles", "tradestats"} <= set(frames_by_ds):
                issues = cross_check_candles_tradestats(frames_by_ds["candles"], frames_by_ds["tradestats"])
                _report_quality(summary, name, "candles+tradestats", issues, f"{name} / свечи ↔ TradeStats", log)
    for_cross.clear()

    for dataset_key, frames in combined.items():
        merged = _concat(frames)
        if merged.empty:
            continue
        if job.sort_rows:
            time_cols = DATASETS[dataset_key].time_columns
            cols = [c for c in ("ticker", "secid", *time_cols) if c in merged.columns]
            if cols:
                try:
                    merged = merged.sort_values(cols, kind="stable").reset_index(drop=True)
                except TypeError:
                    pass
        period = job.candle_period if DATASETS[dataset_key].requires_period else ""
        spans = combined_spans.get(dataset_key) or [(job.start, job.end)]
        span_start = min(s for s, _ in spans)
        span_end = max(e for _, e in spans)
        try:
            out_path = _write_result(
                job, "ALL", dataset_key, period, merged, span_start, span_end, combined=True
            )
        except Exception as exc:  # noqa: BLE001
            err = classify(exc)
            log("error", f"Общий файл {dataset_key}: {err}")
            summary.results.append(
                ExportResult(
                    "ALL", dataset_key, "error", 0, 0, 0, None, err.code, str(err),
                    span_start.isoformat(), span_end.isoformat(),
                )
            )
            continue
        summary.results.append(
            ExportResult(
                "ALL", dataset_key, "ok", len(merged), 0, 0, str(out_path), "", "Объединённый файл",
                span_start.isoformat(), span_end.isoformat(),
            )
        )
        log("ok", f"✔ Общий файл {dataset_key}: {len(merged)} строк → {out_path}")

    summary.finished_at = datetime.now().isoformat(timespec="seconds")
    progress(total_steps, total_steps, "Готово")

    if job.write_manifest:
        try:
            manifest = _write_manifest(job, summary)
            log("info", f"Отчёт о выгрузке: {manifest}")
        except Exception as exc:  # noqa: BLE001
            log("warn", f"Не удалось сохранить отчёт: {classify(exc)}")

    log(
        "info",
        f"Итог: успешно {summary.ok}, пусто {summary.empty}, ошибок {summary.failed}, "
        f"строк всего {summary.total_rows}",
    )
    return summary


def _plan_pair(
    job: ExportJob,
    ticker: str,
    dataset_key: str,
    series: dict,
    log: LogCallback,
    summary: ExportSummary,
    stop: StopCheck,
) -> _Plan | None:
    dataset = DATASETS[dataset_key]
    label = f"{ticker} / {dataset.label}"
    period = job.candle_period if dataset.requires_period else ""

    contracts: list[Contract] = []
    name = ticker
    if job.continuous:
        asset, contracts = _contracts_for(job, ticker, series, log)
        name = output_name(asset)
        if not contracts:
            raise ExportError(
                "not_found", f"Не найдено контрактов фьючерса «{ticker}» для склейки.", False,
                "Укажите код базового актива (например, Si) или контракт из списка.",
            )

    existing = _load_existing(job, name, dataset_key, period, log) if job.update_existing else None
    if existing is not None:
        today = moscow_today()
        if dataset.trading_rows:
            # Последний день в файле мог быть сохранён неполным, качаем его заново.
            last_day = existing[1]
            keep = existing[0]
            days = pd.to_datetime(keep[dataset.date_column], errors="coerce").dt.date
            frame = keep[days < last_day].reset_index(drop=True)
            span = (last_day, max(last_day, today))
        else:
            # Дивиденды и сплиты мелкие, проще перекачать целиком.
            frame = existing[0].iloc[0:0]
            span = (existing[2], today)
        plan_existing = _Existing(existing[3], frame, existing[2])
        log("info", f"  {name} / {dataset.label}: в файле данные по {existing[1]}, догружаю {span[0]} — {span[1]}")
    else:
        plan_existing = None
        if job.continuous and job.full_history:
            # Контракт начинает торговаться примерно за квартал до экспирации.
            first = select_contracts(contracts, job.quarterly_only)[0].last_trade_date
            span = (first - timedelta(days=92), moscow_today())
        elif job.continuous:
            span = (job.start, job.end)
        else:
            span = _resolve_range(job, ticker, dataset_key, log, summary, stop)
        if span is None:
            summary.results.append(
                ExportResult(
                    ticker, dataset_key, "empty",
                    message="У инструмента нет доступной истории по этому набору данных",
                )
            )
            log("warn", f"  {label}: доступной истории нет")
            return None

    start, end = span
    chunk_days = job.chunk_days_for(dataset_key)
    if job.continuous:
        segments = build_schedule(
            select_contracts(contracts, job.quarterly_only), start, end, job.roll_days
        )
        chunks = [
            (s, e, seg.secid)
            for seg in segments
            for s, e in split_range(seg.start, seg.end, chunk_days, job.skip_weekends)
        ]
        if segments:
            log(
                "info",
                f"  {name}: контракты " + ", ".join(f"{seg.secid} ({seg.start} — {seg.end})" for seg in segments),
            )
    else:
        chunks = [(s, e, ticker) for s, e in split_range(start, end, chunk_days, job.skip_weekends)]
    return _Plan(ticker, dataset_key, name, start, end, chunks, plan_existing)


def _contracts_for(job: ExportJob, ticker: str, series: dict, log: LogCallback) -> tuple[str, list[Contract]]:
    cached = series.get(ticker)
    if isinstance(cached, ExportError):
        raise cached
    if cached is None:
        try:
            cached = client.futures_contracts(ticker, job.api_key)
        except Exception as exc:  # noqa: BLE001
            err = classify(exc)
            series[ticker] = err
            raise err from exc
        series[ticker] = cached
        asset, contracts = cached
        log("info", f"  {ticker}: актив {asset}, контрактов в серии: {len({c.secid for c in contracts})}")
    return cached


def _filter_chunks(chunks: list[tuple[date, date, str]], is_trading) -> list[tuple[date, date, str]]:
    result = []
    for start, end, secid in chunks:
        result.extend((s, e, secid) for s, e in drop_non_trading([(start, end)], is_trading))
    return result


def _tag_contract(frame: pd.DataFrame, secid: str, job: ExportJob) -> pd.DataFrame:
    if not job.continuous or frame.empty:
        return frame
    return frame.assign(contract=secid)


def _find_existing(job: ExportJob, name: str, dataset_key: str, period: str) -> Path | None:
    directory = _out_dir_for(job, name)
    if not directory.is_dir():
        return None
    start_mark, end_mark = "§S§", "§E§"
    filename = writer.build_filename(
        job.filename_template, ticker=name, dataset=dataset_key, period=period,
        start=start_mark, end=end_mark, market=job.market,
    )
    pattern = writer.target_path(directory, filename, job.file_format, job.csv_gzip).name
    pattern = pattern.replace(start_mark, "*").replace(end_mark, "*")
    candidates = [p for p in directory.glob(pattern) if p.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _read_existing(job: ExportJob, path: Path) -> pd.DataFrame:
    if job.file_format == "parquet":
        return pd.read_parquet(path)
    return pd.read_csv(
        path, sep=job.csv_sep, decimal=job.csv_decimal, encoding=job.csv_encoding, compression="infer"
    )


def _load_existing(job: ExportJob, name: str, dataset_key: str, period: str, log: LogCallback):
    dataset = DATASETS[dataset_key]
    path = _find_existing(job, name, dataset_key, period)
    if path is None:
        return None
    try:
        frame = _read_existing(job, path)
    except Exception as exc:  # noqa: BLE001
        log("warn", f"  {name} / {dataset.label}: не удалось прочитать {path.name} — {exc}; качаю заново")
        return None
    if dataset.date_column not in frame.columns or frame.empty:
        log("warn", f"  {name} / {dataset.label}: в {path.name} нет колонки {dataset.date_column}; качаю заново")
        return None
    days = pd.to_datetime(frame[dataset.date_column], errors="coerce").dropna()
    if days.empty:
        return None
    derived = [c for c in frame.columns if str(c).startswith("adj_")]
    frame = frame.drop(columns=derived)
    return frame, days.max().date(), days.min().date(), path


def _adjust_candles(
    job: ExportJob,
    plan: _Plan,
    candles: pd.DataFrame,
    label: str,
    log: LogCallback,
    summary: ExportSummary,
    stop: StopCheck,
) -> pd.DataFrame:
    if job.continuous and job.roll_adjust != "none" and "contract" in candles.columns:
        gaps = _roll_gaps(job, candles, log, stop)
        for gap in gaps:
            log("info", f"  {label}: шов {gap.old} → {gap.new} с {gap.boundary.date()}: "
                        f"{gap.old_close:g} → {gap.new_close:g}")
        return apply_roll_gaps(candles, gaps, job.roll_adjust)

    if job.adjust_prices and job.market == "shares":
        frames = {}
        for kind, loader, what in (
            ("dividends", client.dividends, "дивиденды"),
            ("splits", client.splits, "сплиты"),
        ):
            try:
                frames[kind] = loader(plan.ticker, job.api_key)
            except Exception as exc:  # noqa: BLE001
                err = classify(exc)
                warning = f"{label}: {what} не учтены — {err}"
                summary.warnings.append(warning)
                log("warn", "  " + warning)
                frames[kind] = pd.DataFrame()
        divs, spl = frames["dividends"], frames["splits"]
        events = corporate_events(candles, divs, spl)
        for event in events:
            log("info", f"  {label}: {event.note} → множитель {event.factor:.6f}")
        if not events:
            log("info", f"  {label}: корпоративных событий за период нет")
        return apply_factors(candles, events)
    return candles


def _roll_gaps(job: ExportJob, candles: pd.DataFrame, log: LogCallback, stop: StopCheck) -> list[RollGap]:
    # Гэп на шве считаем по дневным закрытиям обоих контрактов в последний общий день.
    stamp = pd.to_datetime(candles["begin"], errors="coerce")
    contract = candles["contract"].astype(str)
    change = contract.ne(contract.shift()) & contract.shift().notna()
    gaps: list[RollGap] = []
    for i in candles.index[change]:
        position = candles.index.get_loc(i)
        old = contract.iloc[position - 1]
        new = contract.iloc[position]
        boundary = stamp.iloc[position].normalize()
        last_old_day = stamp.iloc[position - 1].date()
        window = (last_old_day - timedelta(days=10), last_old_day)
        closes = {}
        for secid in (old, new):
            try:
                daily = _fetch_with_retries(job, secid, "candles", window[0], window[1], "1D", log, stop)
            except ExportError as err:
                log("warn", f"  шов {old} → {new}: нет дневных цен {secid} — {err}")
                daily = None
            if daily is None or daily.empty or "close" not in daily.columns:
                closes = {}
                break
            days = pd.to_datetime(daily["begin"], errors="coerce").dt.normalize()
            closes[secid] = daily.assign(_d=days).groupby("_d")["close"].last()
        if len(closes) != 2:
            continue
        common = closes[old].index.intersection(closes[new].index)
        if common.empty:
            log("warn", f"  шов {old} → {new}: нет общего торгового дня, гэп не убран")
            continue
        day = common.max()
        gaps.append(RollGap(boundary, old, new, float(closes[old][day]), float(closes[new][day])))
    return gaps


def _report_quality(
    summary: ExportSummary, name: str, dataset_key: str, issues: list, label: str, log: LogCallback
) -> None:
    for issue in issues:
        summary.quality.append({"ticker": name, "dataset": dataset_key, **issue.to_dict()})
        level = {"error": "error", "warn": "warn"}.get(issue.level, "info")
        examples = f" (напр.: {', '.join(issue.examples[:3])})" if issue.examples else ""
        log(level, f"  {label}: проверка [{issue.check}] {issue.message}: {issue.count}{examples}")


def _fetch_with_retries(
    job: ExportJob,
    ticker: str,
    dataset_key: str,
    chunk_start: date,
    chunk_end: date,
    period: str,
    log: LogCallback,
    stop: StopCheck,
) -> pd.DataFrame | None:
    attempt = 0
    while True:
        try:
            return client.fetch_chunk(
                job.market, ticker, dataset_key, chunk_start, chunk_end, period, job.api_key
            )
        except Exception as exc:  # noqa: BLE001
            err = classify(exc)
            if not err.retryable or attempt >= job.retries:
                raise err from exc
            attempt += 1
            pause = job.retry_pause * attempt
            log("warn", f"  повтор {attempt}/{job.retries} через {pause:.0f} c — {err.message}")
            waited = 0.0
            while waited < pause:
                if stop():
                    return None
                time.sleep(min(0.25, pause - waited))
                waited += 0.25


def _resolve_range(
    job: ExportJob,
    ticker: str,
    dataset_key: str,
    log: LogCallback,
    summary: ExportSummary,
    stop: StopCheck,
) -> tuple[date, date] | None:
    if not job.full_history:
        return job.start, job.end

    label = f"{ticker} / {DATASETS[dataset_key].label}"
    attempt = 0
    while True:
        try:
            detected = client.detect_history_range(
                job.market, ticker, dataset_key, job.api_key, period=job.candle_period
            )
            break
        except Exception as exc:  # noqa: BLE001
            err = classify(exc)
            if err.code in ("auth", "forbidden", "no_api_key"):
                raise err from exc
            if err.retryable and attempt < job.retries and not stop():
                attempt += 1
                log("warn", f"  {label}: повтор разведки {attempt}/{job.retries} — {err.message}")
                time.sleep(job.retry_pause * attempt)
                continue
            warning = (
                f"{label}: не удалось определить глубину истории — {err} "
                f"Беру диапазон из настроек: {job.start} — {job.end}."
            )
            summary.warnings.append(warning)
            log("warn", "  " + warning)
            return job.start, job.end

    if detected is None:
        return None

    span_start, span_end = detected
    log("info", f"  {label}: доступная история {span_start} — {span_end}")
    return span_start, span_end


def _load_calendar(
    job: ExportJob, spans: list[tuple[date, date]], log: LogCallback, summary: ExportSummary
):
    start = min(s for s, _ in spans)
    end = max(e for _, e in spans)
    try:
        calendar = client.trading_calendar(job.market, start, end, job.api_key)
    except Exception as exc:  # noqa: BLE001
        err = classify(exc)
        warning = f"Торговый календарь MOEX недоступен — {err} Выгрузка идёт без сверки с ним."
        summary.warnings.append(warning)
        log("warn", warning)
        return None
    if calendar is None:
        log("warn", "Торговый календарь MOEX: в ответе нет данных о торговых днях, сверка отключена.")
        return None
    info = calendar.summary()
    source = {"algopack": "шлюз Algopack", "public": "публичный ISS"}.get(calendar.source, calendar.source)
    log(
        "info",
        f"Торговый календарь MOEX ({source}): {calendar.start} — {calendar.end}, торговых дней "
        f"{info['trading_days']}, из них выходных сессий {info['weekend_sessions']}, "
        f"праздников в будни {info['weekday_holidays']}",
    )
    try:
        frame = calendar.to_frame()
        name = writer.safe_name(f"calendar_{calendar.engine}_{calendar.start}_{calendar.end}")
        path = writer.target_path(job.out_dir, name, job.file_format, job.csv_gzip)
        writer.write_dataframe(
            frame, path, job.file_format, csv_sep=job.csv_sep, csv_decimal=job.csv_decimal,
            csv_encoding=job.csv_encoding, csv_gzip=job.csv_gzip,
            parquet_compression=job.parquet_compression,
        )
        info["path"] = str(path)
        log("info", f"  календарь сохранён: {path}")
    except Exception as exc:  # noqa: BLE001
        log("warn", f"  не удалось сохранить календарь: {classify(exc)}")
    summary.calendar = info
    return calendar


def _check_gaps(
    df: pd.DataFrame,
    dataset: DatasetInfo,
    calendar,
    job: ExportJob,
    label: str,
    log: LogCallback,
    summary: ExportSummary,
) -> str:
    if not dataset.daily_coverage or dataset.date_column not in df.columns:
        return ""
    present = set(pd.to_datetime(df[dataset.date_column], errors="coerce").dropna().dt.date)
    if not present:
        return ""
    first, last = min(present), max(present)
    # Выходные сессии открывали для бумаг постепенно: до первой выходной
    # сессии самой бумаги пустые выходные пропуском не считаем.
    weekend_present = [day for day in present if day.weekday() >= 5]
    first_weekend = min(weekend_present) if weekend_present else None
    missing = [
        day
        for day in calendar.trading_days(first, last)
        if day not in present
        and is_settled(day)
        and not (job.skip_weekends and day.weekday() >= 5)
        and not (day.weekday() >= 5 and (first_weekend is None or day < first_weekend))
    ]
    if not missing:
        return ""
    shown = ", ".join(day.isoformat() for day in missing[:10])
    more = f" и ещё {len(missing) - 10}" if len(missing) > 10 else ""
    warning = (
        f"{label}: нет данных за торговые дни по календарю MOEX ({len(missing)}): {shown}{more}"
    )
    summary.warnings.append(warning)
    log("warn", "  " + warning)
    return f"пропущено торговых дней: {len(missing)}"


def _out_dir_for(job: ExportJob, ticker: str) -> Path:
    return job.out_dir / writer.safe_name(ticker) if job.subfolder_per_ticker else job.out_dir


def _write_result(
    job: ExportJob,
    ticker: str,
    dataset_key: str,
    period: str,
    df: pd.DataFrame,
    span_start: date,
    span_end: date,
    combined: bool = False,
) -> Path:
    directory = job.out_dir if combined else _out_dir_for(job, ticker)
    filename = writer.build_filename(
        job.filename_template,
        ticker=ticker,
        dataset=dataset_key,
        period=period,
        start=span_start.isoformat(),
        end=span_end.isoformat(),
        market=job.market,
    )
    path = writer.target_path(directory, filename, job.file_format, job.csv_gzip)
    return writer.write_dataframe(
        df,
        path,
        job.file_format,
        csv_sep=job.csv_sep,
        csv_decimal=job.csv_decimal,
        csv_encoding=job.csv_encoding,
        csv_gzip=job.csv_gzip,
        parquet_compression=job.parquet_compression,
    )


def _write_part(
    job: ExportJob,
    ticker: str,
    dataset_key: str,
    period: str,
    chunk_start: date,
    chunk_end: date,
    df: pd.DataFrame,
) -> None:
    period_part = f"_{period}" if period else ""
    name = writer.safe_name(f"{ticker}_{dataset_key}{period_part}_{chunk_start}_{chunk_end}")
    directory = _out_dir_for(job, ticker) / "parts" / dataset_key
    path = writer.target_path(directory, name, job.file_format, job.csv_gzip)
    writer.write_dataframe(
        df,
        path,
        job.file_format,
        csv_sep=job.csv_sep,
        csv_decimal=job.csv_decimal,
        csv_encoding=job.csv_encoding,
        csv_gzip=job.csv_gzip,
        parquet_compression=job.parquet_compression,
    )


def _job_to_dict(job: ExportJob) -> dict:
    return {
        "market": job.market,
        "market_label": MARKETS[job.market].label,
        "tickers": job.tickers,
        "datasets": job.datasets,
        "start": job.start.isoformat(),
        "end": job.end.isoformat(),
        "candle_period": job.candle_period,
        "full_history": job.full_history,
        "file_format": job.file_format,
        "out_dir": str(job.out_dir),
        "chunk_mode": job.chunk_mode,
        "chunk_days": job.chunk_days,
        "skip_weekends": job.skip_weekends,
        "retries": job.retries,
        "use_cache": job.use_cache,
        "dedupe": job.dedupe,
        "parse_dates": job.parse_dates,
        "use_calendar": job.use_calendar,
        "validate_data": job.validate_data,
        "adjust_prices": job.adjust_prices,
        "update_existing": job.update_existing,
        "continuous": job.continuous,
        "roll_days": job.roll_days,
        "roll_adjust": job.roll_adjust,
        "quarterly_only": job.quarterly_only,
        "combine_tickers": job.combine_tickers,
    }


def _write_manifest(job: ExportJob, summary: ExportSummary) -> Path:
    job.out_dir.mkdir(parents=True, exist_ok=True)
    stamp = summary.started_at.replace(":", "-")
    path = job.out_dir / f"moexdata_report_{stamp}.json"
    path.write_text(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path
