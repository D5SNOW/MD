from __future__ import annotations

import sys
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from moexdata.api import client  # noqa: E402
from moexdata.api.calendar import TradingCalendar  # noqa: E402
from moexdata.core import exporter  # noqa: E402
from moexdata.core.exporter import ExportJob, run_export  # noqa: E402

failures: list[str] = []
HOLIDAYS = {date(2024, 3, 8)}

_NO_CALENDAR = lambda *args, **kwargs: None  # noqa: E731
client.trading_calendar = _NO_CALENDAR


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"  ok   {message}")
    else:
        failures.append(message)
        print(f"  FAIL {message}")


class FakeApi:
    def __init__(self, fail_dates: set[date] | None = None, bad_key: bool = False):
        self.calls: list[tuple[str, str, date, date]] = []
        self.fail_dates = fail_dates or set()
        self.failed_once: set[date] = set()
        self.bad_key = bad_key

    def __call__(self, market, secid, dataset, start, end, period, api_key):
        self.calls.append((secid, dataset, start, end))
        if self.bad_key:
            raise _http_error(403)
        if start in self.fail_dates and start not in self.failed_once:
            self.failed_once.add(start)
            raise _http_error(503)

        rows = []
        day = start
        while day <= end:
            if day.weekday() < 5 and day not in HOLIDAYS:
                base = datetime.combine(day, time(10, 0))
                for i in range(540):
                    begin = base + timedelta(minutes=i)
                    rows.append(
                        {
                            "begin": begin.isoformat(sep=" "),
                            "end": (begin + timedelta(seconds=59)).isoformat(sep=" "),
                            "open": 100 + i / 100,
                            "close": 100 + i / 100,
                            "volume": 10 * (i + 1),
                        }
                    )
            day += timedelta(days=1)
        return pd.DataFrame(rows[: exporter.ROW_LIMIT])


def _http_error(status: int):
    import httpx

    request = httpx.Request("GET", "https://apim.moex.com/iss/x.json")
    return httpx.HTTPStatusError("boom", request=request, response=httpx.Response(status, request=request))


def make_job(out_dir: Path, **kwargs) -> ExportJob:
    base = dict(
        api_key="test-key",
        market="shares",
        tickers=["SBER"],
        datasets=["candles"],
        start=date(2024, 3, 1),
        end=date(2024, 3, 15),
        out_dir=out_dir,
        candle_period="1min",
        file_format="parquet",
        retry_pause=0.0,
        request_pause=0.0,
        use_cache=False,
        write_manifest=False,
        skip_weekends=True,
    )
    base.update(kwargs)
    return ExportJob(**base)


def run(job: ExportJob, api: FakeApi, verbose: bool = False, history=None, calendar=None):
    original_fetch = client.fetch_chunk
    original_detect = client.detect_history_range
    client.fetch_chunk = api
    if history is not None:
        client.detect_history_range = history
    if calendar is not None:
        client.trading_calendar = calendar
    try:
        return run_export(job, log_cb=(lambda lvl, m: print("    " + m)) if verbose else None)
    finally:
        client.fetch_chunk = original_fetch
        client.detect_history_range = original_detect
        client.trading_calendar = _NO_CALENDAR


def test_daily_chunks_merged_into_one_file() -> None:
    print("Минутные свечи: день на запрос -> один итоговый файл")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        api = FakeApi()
        summary = run(make_job(out), api)

        check(len(api.calls) == 11, f"по одному запросу на будний день: {len(api.calls)}")
        check(all(s == e for _, _, s, e in api.calls), "каждый запрос охватывает ровно один день")
        check(all(s.weekday() < 5 for _, _, s, _ in api.calls), "выходные не запрашиваются")

        files = list(out.rglob("*.parquet"))
        check(len(files) == 1, f"на выходе один готовый файл: {[f.name for f in files]}")
        df = pd.read_parquet(files[0])
        check(len(df) == 5400, f"склеено 10 дней x 540 свечей = {len(df)}")
        check(df["begin"].is_monotonic_increasing, "строки отсортированы по времени")
        check(df["begin"].is_unique, "дубликатов нет")
        check(df["ticker"].iloc[0] == "SBER", "проставлен тикер")
        check(summary.ok == 1 and summary.failed == 0, "статус: успешно")
        check(summary.total_rows == 5400, "в отчёте верное число строк")


def test_retry_on_transient_error() -> None:
    print("Временный сбой 503 -> повтор и полные данные")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        api = FakeApi(fail_dates={date(2024, 3, 5), date(2024, 3, 6)})
        summary = run(make_job(out, retries=2), api)
        df = pd.read_parquet(next(out.rglob("*.parquet")))
        check(len(df) == 5400, f"после повторов данные полные: {len(df)}")
        check(len(api.calls) == 13, f"два запроса повторялись: всего вызовов {len(api.calls)}")
        check(summary.failed == 0, "ошибок в итоге нет")


def test_permanent_error_reported() -> None:
    print("Неверный ключ -> понятная ошибка, выгрузка не падает")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        summary = run(make_job(out, retries=1), FakeApi(bad_key=True))
        check(summary.failed == 1 and summary.ok == 0, "результат помечен как ошибка")
        result = summary.results[0]
        check(result.code == "forbidden", f"код ошибки: {result.code}")
        check("подписки" in result.message or "недействителен" in result.message, "подсказка в сообщении")
        check(not list(out.rglob("*.parquet")), "битый файл не создан")


def test_row_limit_warning() -> None:
    print("Достигнут лимит API -> предупреждение в отчёте")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        job = make_job(out, chunk_mode="manual", chunk_days=60, start=date(2024, 1, 1), end=date(2024, 2, 29))
        summary = run(job, FakeApi())
        check(bool(summary.warnings), "предупреждение о лимите записано")
        check("лимит API" in summary.warnings[0], f"текст предупреждения: {summary.warnings[0][:60]}…")


def test_cache_resume() -> None:
    print("Кеш частей: повторный запуск не дёргает API")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        api1 = FakeApi()
        run(make_job(out, use_cache=True), api1)
        api2 = FakeApi()
        summary = run(make_job(out, use_cache=True), api2)
        check(len(api2.calls) == 0, f"второй запуск без запросов к API: {len(api2.calls)}")
        check(summary.total_rows == 5400, "данные восстановлены из кеша полностью")


def test_stop_requested() -> None:
    print("Остановка пользователем")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        api = FakeApi()
        original = client.fetch_chunk
        client.fetch_chunk = api
        try:
            summary = run_export(make_job(out), should_stop=lambda: len(api.calls) >= 3)
        finally:
            client.fetch_chunk = original
        check(summary.stopped, "выгрузка помечена как остановленная")
        check(len(api.calls) <= 4, f"после остановки запросы прекратились: {len(api.calls)}")


def test_multiple_tickers_and_combine() -> None:
    print("Несколько тикеров и объединение в один файл")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        job = make_job(out, tickers=["SBER", "GAZP"], start=date(2024, 3, 4), end=date(2024, 3, 6))
        run(job, FakeApi())
        files = sorted(f.name for f in out.rglob("*.parquet"))
        check(len(files) == 2, f"по файлу на тикер: {files}")

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        job = make_job(
            out, tickers=["SBER", "GAZP"], start=date(2024, 3, 4), end=date(2024, 3, 6),
            combine_tickers=True, file_format="csv",
        )
        run(job, FakeApi())
        files = list(out.rglob("*.csv"))
        check(len(files) == 1, f"один общий файл: {[f.name for f in files]}")
        df = pd.read_csv(files[0])
        check(set(df["ticker"]) == {"SBER", "GAZP"}, "в общем файле оба тикера")
        check(len(df) == 2 * 3 * 540, f"строк {len(df)} = 2 тикера x 3 дня x 540")


def test_keep_parts_and_manifest() -> None:
    print("По-дневные части и JSON-отчёт")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        job = make_job(out, start=date(2024, 3, 4), end=date(2024, 3, 6), keep_parts=True, write_manifest=True)
        run(job, FakeApi())
        parts = list((out / "SBER" / "parts").rglob("*.parquet"))
        check(len(parts) == 3, f"сохранено частей: {len(parts)}")
        reports = list(out.glob("moexdata_report_*.json"))
        check(len(reports) == 1, "JSON-отчёт записан")
        import json

        data = json.loads(reports[0].read_text(encoding="utf-8"))
        check(data["totals"]["rows"] == 3 * 540, "в отчёте верное число строк")
        check(data["job"]["candle_period"] == "1min", "в отчёте сохранены параметры задания")


def test_non_candle_dataset_chunking() -> None:
    print("TradeStats: крупные части по 30 дней")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        api = FakeApi()
        job = make_job(out, datasets=["tradestats"], start=date(2024, 1, 1), end=date(2024, 3, 31))
        run(job, api)
        spans = {(e - s).days + 1 for _, _, s, e in api.calls}
        check(len(api.calls) == 3, f"квартал -> 3 запроса (получено {len(api.calls)})")
        check(max(spans) <= 30, f"ни одна часть не длиннее 30 дней: {sorted(spans)}")
        check(api.calls[-1][3] == date(2024, 3, 29), f"хвост подрезан до пятницы: {api.calls[-1][3]}")


def test_full_history_uses_detected_range() -> None:
    print("Вся история: диапазон берётся у биржи, а не из полей дат")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        api = FakeApi()
        detected = {
            "candles": (date(2024, 3, 4), date(2024, 3, 6)),
            "tradestats": (date(2024, 2, 1), date(2024, 2, 29)),
        }
        calls: list[tuple[str, str]] = []

        def history(market, secid, dataset_key, api_key, period=""):
            calls.append((secid, dataset_key))
            return detected[dataset_key]

        job = make_job(
            out,
            datasets=["candles", "tradestats"],
            start=date(2024, 1, 2),
            end=date(2024, 1, 3),
            full_history=True,
        )
        summary = run(job, api, history=history)

        check(calls == [("SBER", "candles"), ("SBER", "tradestats")], f"разведка по каждой паре: {calls}")
        candle_days = sorted({s for _, ds, s, _ in api.calls if ds == "candles"})
        check(
            candle_days == [date(2024, 3, 4), date(2024, 3, 5), date(2024, 3, 6)],
            f"свечи качались за найденный период: {candle_days}",
        )
        names = sorted(path.name for path in (out / "SBER").glob("*.parquet"))
        check(
            names == [
                "SBER_candles_1min_2024-03-04_2024-03-06.parquet",
                "SBER_tradestats_2024-02-01_2024-02-29.parquet",
            ],
            f"в именах файлов найденный диапазон: {names}",
        )
        ranges = {r.dataset: (r.start, r.end) for r in summary.results}
        check(
            ranges["tradestats"] == ("2024-02-01", "2024-02-29"),
            f"диапазон записан в отчёт: {ranges}",
        )


def test_full_history_without_data() -> None:
    print("Вся история: у инструмента нет данных набора")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        api = FakeApi()
        job = make_job(out, full_history=True)
        summary = run(job, api, history=lambda *a, **k: None)
        check(not api.calls, f"запросов за данными не было: {len(api.calls)}")
        check(summary.empty == 1 and summary.failed == 0, "пара помечена как «нет данных»")


def test_full_history_probe_failure_falls_back() -> None:
    print("Вся история: разведка не удалась -> берётся период из настроек")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        api = FakeApi()

        def history(*args, **kwargs):
            raise _http_error(503)

        job = make_job(
            out, start=date(2024, 3, 4), end=date(2024, 3, 6), full_history=True
        )
        summary = run(job, api, history=history)
        check(len(api.calls) == 3, f"выгрузка пошла по датам из настроек: {len(api.calls)}")
        check(any("глубину истории" in w for w in summary.warnings), "в отчёт попало предупреждение")


def test_full_history_stops_on_bad_key() -> None:
    print("Вся история: отказ по ключу прерывает разведку")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        api = FakeApi()

        def history(*args, **kwargs):
            raise _http_error(403)

        job = make_job(out, tickers=["SBER", "GAZP"], full_history=True)
        summary = run(job, api, history=history)
        check(not api.calls, "к данным не обращались")
        check(summary.failed == 1, f"одна запись об ошибке, а не по одной на тикер: {summary.failed}")
        check(summary.results[0].code == "forbidden", f"код ошибки сохранён: {summary.results[0].code}")


def test_weekend_sessions_by_default() -> None:
    print("По умолчанию выходные сессии тоже запрашиваются")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        api = FakeApi()
        job = make_job(out)
        job.skip_weekends = ExportJob.skip_weekends
        run(job, api)
        days = {s for _, _, s, _ in api.calls}
        check(len(days) == 15, f"1—15 марта: запрос на каждый календарный день ({len(days)})")
        check(date(2024, 3, 2) in days, "суббота запрошена")


def test_open_day_not_cached() -> None:
    print("Незакрытый торговый день не попадает в кеш")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        today = exporter.moscow_today()
        start = today - timedelta(days=6)
        settled = exporter.is_settled
        exporter.is_settled = lambda day, now=None: day < today
        try:
            job = make_job(out, use_cache=True, skip_weekends=False, start=start, end=today)
            summary = run(job, FakeApi())
            api2 = FakeApi()
            run(make_job(out, use_cache=True, skip_weekends=False, start=start, end=today), api2)
        finally:
            exporter.is_settled = settled
        check(
            [s for _, _, s, _ in api2.calls] == [today],
            f"повторно запрошен только сегодняшний день: {[str(s) for _, _, s, _ in api2.calls]}",
        )
        check(
            any("незакрытый торговый день" in w for w in summary.warnings),
            "в отчёте предупреждение о неполном дне",
        )


def test_futoi_has_no_row_limit() -> None:
    print("FUTOI: библиотека дочитывает страницы сама -> лимит не предупреждаем")

    def futoi_api(market, secid, dataset, start, end, period, api_key):
        rows = [
            {"tradedate": str(start), "tradetime": f"{i // 360:02d}:{i // 6 % 60:02d}:{i % 6 * 10:02d}",
             "clgroup": g, "pos": i}
            for i in range(6000)
            for g in ("FIZ", "YUR")
        ]
        return pd.DataFrame(rows)

    with tempfile.TemporaryDirectory() as tmp:
        job = make_job(
            Path(tmp), market="futures", tickers=["SiZ5"], datasets=["futoi"],
            start=date(2024, 3, 4), end=date(2024, 3, 4),
        )
        summary = run(job, futoi_api)
        check(summary.total_rows == 12000, f"все строки на месте: {summary.total_rows}")
        check(not summary.warnings, f"ложного предупреждения о лимите нет: {summary.warnings}")


def _calendar(start: date, end: date, extra: dict | None = None):
    days = {date(2024, 3, 8): False}
    days.update(extra or {})

    def load(market, s, e, api_key):
        return TradingCalendar("stock", start, end, dict(days))

    return load


def test_calendar_skips_non_trading_days() -> None:
    print("Торговый календарь: неторговые дни не запрашиваются")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        api = FakeApi()
        job = make_job(out, skip_weekends=False)
        summary = run(job, api, calendar=_calendar(date(2024, 1, 1), date(2024, 12, 31)))
        days = sorted(s for _, _, s, _ in api.calls)
        check(len(days) == 10, f"15 календарных дней -> 10 торговых запросов ({len(days)})")
        check(date(2024, 3, 8) not in days, "праздник 8 марта не запрошен")
        check(date(2024, 3, 2) not in days, "выходной без сессии не запрошен")
        check(not summary.warnings, f"пропусков нет — предупреждений нет: {summary.warnings}")
        check(summary.total_rows == 5400, "данные полные")


def test_calendar_weekend_session_and_gaps() -> None:
    print("Торговый календарь: выходная сессия запрашивается, пропуски находятся")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        api = FakeApi()
        cal = _calendar(
            date(2024, 1, 1), date(2024, 12, 31),
            {date(2024, 3, 2): True, date(2024, 3, 8): True},
        )
        summary = run(make_job(out, skip_weekends=False), api, calendar=cal)
        days = {s for _, _, s, _ in api.calls}
        check(date(2024, 3, 2) in days, "торговая суббота запрошена")
        gaps = [w for w in summary.warnings if "нет данных за торговые дни" in w]
        check(len(gaps) == 1 and "2024-03-08" in gaps[0], f"пропущенный будний торговый день назван: {gaps}")
        check("2024-03-02" not in gaps[0], "суббота до первой выходной сессии бумаги — не пропуск")
        check(
            summary.results[0].message.startswith("пропущено торговых дней: 1"),
            f"пометка в отчёте: {summary.results[0].message}",
        )
        check(summary.calendar.get("weekend_sessions") == 1, f"сводка календаря в отчёте: {summary.calendar}")
        check(any(p.name.startswith("calendar_stock_") for p in out.glob("*.parquet")), "календарь сохранён файлом")


def test_calendar_failure_is_not_fatal() -> None:
    print("Торговый календарь недоступен -> выгрузка идёт как раньше")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        api = FakeApi()

        def broken(*args, **kwargs):
            raise _http_error(503)

        summary = run(make_job(out), api, calendar=broken)
        check(summary.ok == 1 and summary.total_rows == 5400, "данные выгружены")
        check(any("календарь" in w for w in summary.warnings), "в отчёте предупреждение о календаре")


class ContractApi(FakeApi):
    def __call__(self, market, secid, dataset, start, end, period, api_key):
        frame = super().__call__(market, secid, dataset, start, end, period, api_key)
        if secid == "SiM4" and not frame.empty:
            frame["close"] = frame["close"] + 10
            frame["open"] = frame["open"] + 10
        return frame


def test_continuous_futures() -> None:
    from moexdata.core.continuous import Contract

    print("Склейка фьючерсов: контракты по расписанию + сдвиг на швах")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        api = ContractApi()
        original = client.futures_contracts
        client.futures_contracts = lambda name, key: (
            "Si", [Contract("SiH4", date(2024, 3, 21)), Contract("SiM4", date(2024, 6, 20))]
        )
        try:
            job = make_job(
                out, market="futures", tickers=["SiH4"], start=date(2024, 3, 11), end=date(2024, 3, 22),
                continuous=True, roll_days=5,
            )
            summary = run(job, api)
        finally:
            client.futures_contracts = original
        minute_calls = [(secid, s) for secid, ds, s, e in api.calls if s == e]
        check(
            {s for secid, s in minute_calls if secid == "SiH4"} >= {date(2024, 3, 11), date(2024, 3, 14)}
            and all(s <= date(2024, 3, 14) for secid, s in minute_calls if secid == "SiH4"),
            "старый контракт качается до дня перехода включительно",
        )
        check(
            all(s >= date(2024, 3, 15) for secid, s in minute_calls if secid == "SiM4"),
            "новый контракт — со следующего дня",
        )
        files = list(out.rglob("*.parquet"))
        check([f.name for f in files] == ["Si_cont_candles_1min_2024-03-11_2024-03-22.parquet"], f"имя ряда: {[f.name for f in files]}")
        df = pd.read_parquet(files[0])
        check(set(df["contract"]) == {"SiH4", "SiM4"}, "колонка contract заполнена")
        old = df[df["contract"] == "SiH4"]
        new = df[df["contract"] == "SiM4"]
        check(bool(((old["adj_close"] - old["close"]).round(6) == 10).all()), "история старого контракта сдвинута на гэп")
        check(bool((new["adj_close"] == new["close"]).all()), "текущий контракт без сдвига")
        check(summary.results[0].ticker == "SiH4", "в отчёте тикер из задания — для «Повторить неудачные»")


def test_dividend_adjustment_in_export() -> None:
    print("Акции: цены с поправкой на дивиденды")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        originals = client.dividends, client.splits
        client.dividends = lambda secid, key: pd.DataFrame(
            {"registryclosedate": ["2024-03-12"], "value": [5.0], "currencyid": ["SUR"], "last_buy_date": ["2024-03-11"]}
        )
        client.splits = lambda secid, key: pd.DataFrame()
        try:
            run(make_job(out, adjust_prices=True), FakeApi())
        finally:
            client.dividends, client.splits = originals
        df = pd.read_parquet(next(out.rglob("*.parquet")))
        day = pd.to_datetime(df["begin"]).dt.date
        factor = 1 - 5.0 / 105.39
        before = df[day == date(2024, 3, 11)]
        after = df[day == date(2024, 3, 12)]
        check(bool(((before["adj_close"] - before["close"] * factor).abs() < 1e-6).all()), "до гэпа цены умножены на множитель")
        check(bool((after["adj_close"] == after["close"]).all()), "после гэпа — биржевые цены")


def test_split_adjustment_without_dividends() -> None:
    from moexdata.errors import ExportError

    print("Акции: дивиденды недоступны -> поправка только на сплиты, с предупреждением")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        originals = client.dividends, client.splits

        def no_dividends(secid, key):
            raise ExportError("unsupported", "Дивиденды недоступны: нужна подписка (403).", False, "")

        client.dividends = no_dividends
        client.splits = lambda secid, key: pd.DataFrame(
            {"tradedate": [pd.Timestamp("2024-03-12")], "before": [1], "after": [10]}
        )
        try:
            summary = run(make_job(out, adjust_prices=True), FakeApi())
        finally:
            client.dividends, client.splits = originals
        df = pd.read_parquet(next(out.rglob("*.parquet")))
        day = pd.to_datetime(df["begin"]).dt.date
        before = df[day == date(2024, 3, 11)]
        check(bool(((before["adj_close"] - before["close"] * 0.1).abs() < 1e-9).all()), "сплит 1:10 применён")
        check(any("дивиденды не учтены" in w for w in summary.warnings), f"предупреждение о дивидендах: {summary.warnings}")


def test_dividends_unavailable_is_not_failure() -> None:
    from moexdata.errors import ExportError

    print("Дивиденды без источника: не ошибка выгрузки, а «нет данных» с причиной")

    def api(market, secid, dataset, start, end, period, key):
        raise ExportError("unsupported", "Дивиденды недоступны: нужна подписка (403).", False, "")

    with tempfile.TemporaryDirectory() as tmp:
        summary = run(make_job(Path(tmp), datasets=["dividends"]), api)
        result = summary.results[0]
        check(result.status == "empty" and summary.failed == 0, f"статус: {result.status}, ошибок: {summary.failed}")
        check(result.code == "unsupported" and "подписка" in result.message, f"причина сохранена: {result.message}")
        check(not summary.failed_pairs(), "«Повторить неудачные» не будет её перезапускать")


def test_dividends_dataset() -> None:
    print("Набор «Дивиденды»: один запрос на весь период, выходные не фильтруются")
    calls = []

    def api(market, secid, dataset, start, end, period, key):
        calls.append((start, end))
        return pd.DataFrame(
            {"secid": ["SBER", "SBER"], "registryclosedate": ["2024-03-02", "2024-03-12"], "value": [1.0, 2.0]}
        )

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        job = make_job(out, datasets=["dividends"], start=date(2024, 1, 1), end=date(2024, 12, 31), skip_weekends=True)
        summary = run(job, api)
        check(len(calls) == 1, f"один запрос: {calls}")
        check(summary.total_rows == 2, f"субботний реестр не выброшен: {summary.total_rows}")


def test_quality_in_report() -> None:
    print("Проверка качества попадает в отчёт")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        summary = run(make_job(out, write_manifest=True), FakeApi())
        check(any(q["check"] == "price_jump" for q in summary.quality), f"скачки найдены: {summary.quality[:1]}")
        import json

        report = json.loads(next(out.glob("moexdata_report_*.json")).read_text(encoding="utf-8"))
        check("quality" in report and report["quality"], "раздел quality в JSON-отчёте")
        check("проверка" in summary.results[0].message, f"итог проверки в сообщении: {summary.results[0].message}")


def test_update_existing() -> None:
    print("Догрузка до сегодняшнего дня")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        original_today = exporter.moscow_today
        exporter.moscow_today = lambda: date(2024, 3, 15)
        try:
            run(make_job(out, start=date(2024, 3, 1), end=date(2024, 3, 7)), FakeApi())
            first = sorted(p.name for p in out.rglob("*.parquet"))
            api = FakeApi()
            summary = run(
                make_job(out, start=date(2024, 1, 1), end=date(2024, 1, 2), update_existing=True), api
            )
        finally:
            exporter.moscow_today = original_today
        check(first == ["SBER_candles_1min_2024-03-01_2024-03-07.parquet"], f"исходный файл: {first}")
        days = sorted({s for _, _, s, _ in api.calls})
        check(days[0] == date(2024, 3, 7) and days[-1] == date(2024, 3, 15), f"качается с последнего дня файла: {days[0]} — {days[-1]}")
        files = sorted(p.name for p in out.rglob("*.parquet"))
        check(files == ["SBER_candles_1min_2024-03-01_2024-03-15.parquet"], f"файл переименован, старый удалён: {files}")
        df = pd.read_parquet(next(out.rglob("*.parquet")))
        check(len(df) == 5400, f"склеено без дублей: {len(df)}")
        check(df["begin"].is_unique and df["begin"].is_monotonic_increasing, "время уникально и по порядку")
        check(summary.results[0].start == "2024-03-01", "в отчёте диапазон от начала файла")


def main() -> int:
    for test in (
        test_daily_chunks_merged_into_one_file,
        test_retry_on_transient_error,
        test_permanent_error_reported,
        test_row_limit_warning,
        test_cache_resume,
        test_stop_requested,
        test_multiple_tickers_and_combine,
        test_keep_parts_and_manifest,
        test_non_candle_dataset_chunking,
        test_full_history_uses_detected_range,
        test_full_history_without_data,
        test_full_history_probe_failure_falls_back,
        test_full_history_stops_on_bad_key,
        test_weekend_sessions_by_default,
        test_open_day_not_cached,
        test_futoi_has_no_row_limit,
        test_calendar_skips_non_trading_days,
        test_calendar_weekend_session_and_gaps,
        test_calendar_failure_is_not_fatal,
        test_continuous_futures,
        test_dividend_adjustment_in_export,
        test_dividends_dataset,
        test_dividends_unavailable_is_not_failure,
        test_split_adjustment_without_dividends,
        test_quality_in_report,
        test_update_existing,
    ):
        test()
        print()
    if failures:
        print(f"ПРОВАЛЕНО проверок: {len(failures)}")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("Все проверки конвейера пройдены.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
