from __future__ import annotations

import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from moexdata.api import client  # noqa: E402
from moexdata.api.datasets import DATASETS, auto_chunk_days, source_period  # noqa: E402
from moexdata.core import writer  # noqa: E402
from moexdata.api.calendar import TradingCalendar, merge_pages, parse_calendar  # noqa: E402
from moexdata.core.chunking import drop_non_trading, split_range  # noqa: E402
from moexdata.core.exporter import ExportJob, postprocess  # noqa: E402
from moexdata.errors import ConfigError, classify  # noqa: E402
from moexdata.settings import Settings  # noqa: E402
from moexdata.timeutil import MOSCOW_TZ, is_settled  # noqa: E402

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"  ok   {message}")
    else:
        failures.append(message)
        print(f"  FAIL {message}")


def test_chunking() -> None:
    print("Разбиение диапазона:")
    days = split_range(date(2024, 1, 1), date(2024, 1, 7), 1, skip_weekends=True)
    check(len(days) == 5, f"неделя по дням без выходных -> 5 запросов (получено {len(days)})")
    check(all(s == e for s, e in days), "каждый чанк — один день")

    all_days = split_range(date(2024, 1, 1), date(2024, 1, 7), 1, skip_weekends=False)
    check(len(all_days) == 7, f"неделя по дням с выходными -> 7 (получено {len(all_days)})")

    weekly = split_range(date(2024, 1, 1), date(2024, 1, 31), 7, skip_weekends=True)
    check(weekly[0] == (date(2024, 1, 1), date(2024, 1, 5)), f"первый чанк обрезан по будням: {weekly[0]}")
    check(weekly[-1][1] == date(2024, 1, 31), "последний чанк заканчивается на дате конца")

    whole = split_range(date(2020, 1, 1), date(2024, 1, 1), 0, skip_weekends=True)
    check(whole == [(date(2020, 1, 1), date(2024, 1, 1))], "chunk_days=0 -> один запрос")

    covered = split_range(date(2024, 2, 1), date(2024, 2, 29), 5, skip_weekends=False)
    check(covered[0][0] == date(2024, 2, 1) and covered[-1][1] == date(2024, 2, 29), "покрыт весь диапазон")
    contiguous = all(
        (covered[i][1] - covered[i + 1][0]).days == -1 for i in range(len(covered) - 1)
    )
    check(contiguous, "чанки идут подряд без пропусков и нахлёстов")


def test_auto_chunks() -> None:
    print("Авто-размер части выгрузки:")
    check(auto_chunk_days("candles", "1min") == 1, "минутные свечи -> 1 день на запрос (требование ТЗ)")
    check(auto_chunk_days("candles", "1h") == 60, "часовые свечи -> 60 дней")
    check(auto_chunk_days("candles", "1M") == 0, "месячные свечи -> одним запросом")
    check(auto_chunk_days("tradestats", "1min") == 30, "tradestats -> 30 дней независимо от периода")


def test_validation() -> None:
    print("Проверка параметров задания:")

    def job(**kwargs) -> ExportJob:
        base = dict(
            api_key="x",
            market="shares",
            tickers=["SBER"],
            datasets=["candles"],
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
            out_dir=Path(tempfile.gettempdir()),
            file_format="csv",
        )
        base.update(kwargs)
        return ExportJob(**base)

    def expect_error(kwargs: dict, what: str) -> None:
        try:
            job(**kwargs).validate()
        except ConfigError:
            print(f"  ok   отклонено: {what}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{what}: неожиданное исключение {exc!r}")
            print(f"  FAIL {what}: {exc!r}")
        else:
            failures.append(f"{what}: ошибка не поднята")
            print(f"  FAIL {what}: ошибка не поднята")

    expect_error({"api_key": "  "}, "пустой API-ключ")
    expect_error({"tickers": []}, "пустой список тикеров")
    expect_error({"datasets": []}, "пустой список наборов")
    expect_error({"datasets": ["futoi"]}, "futoi на рынке акций")
    expect_error({"datasets": ["nope"]}, "несуществующий набор")
    expect_error({"start": date(2024, 5, 1), "end": date(2024, 1, 1)}, "начало позже конца")
    expect_error({"end": date(2100, 1, 1)}, "дата в будущем")
    expect_error({"file_format": "xlsx"}, "неподдерживаемый формат")
    expect_error({"market": "nope"}, "неизвестный рынок")

    try:
        job().validate()
        print("  ok   корректное задание проходит проверку")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"корректное задание отклонено: {exc!r}")
        print(f"  FAIL корректное задание отклонено: {exc!r}")


def test_postprocess() -> None:
    print("Склейка и постобработка:")
    day1 = pd.DataFrame(
        {"begin": ["2024-01-02 10:01:00", "2024-01-02 10:02:00"], "close": [1.0, 2.0]}
    )
    day2 = pd.DataFrame(
        {"begin": ["2024-01-02 10:02:00", "2024-01-03 10:00:00"], "close": [2.0, 3.0]}
    )
    merged = pd.concat([day1, day2], ignore_index=True)
    job = ExportJob(
        api_key="x", market="shares", tickers=["SBER"], datasets=["candles"],
        start=date(2024, 1, 2), end=date(2024, 1, 3), out_dir=Path(tempfile.gettempdir()),
    )
    result = postprocess(merged, "candles", "SBER", job)
    check(len(result) == 3, f"дубликат на стыке дней удалён (строк {len(result)})")
    check(list(result["begin"]) == sorted(result["begin"]), "строки отсортированы по времени")
    check(result.columns[0] == "ticker", "добавлена колонка ticker первой")
    check(set(result["ticker"]) == {"SBER"}, "колонка ticker заполнена")

    futoi = pd.DataFrame(
        {
            "tradedate": ["2024-01-02"] * 4,
            "tradetime": ["10:00:00", "10:00:00", "10:05:00", "10:05:00"],
            "clgroup": ["FIZ", "YUR", "FIZ", "YUR"],
            "pos": [10, 20, 11, 21],
        }
    )
    kept = postprocess(pd.concat([futoi, futoi], ignore_index=True), "futoi", "SiZ5", job)
    check(len(kept) == 4, f"строки с одной меткой времени сохранены (осталось {len(kept)})")
    check(set(kept["clgroup"]) == {"FIZ", "YUR"}, "обе группы участников на месте")
    check(list(kept["pos"]) == [10, 20, 11, 21], "порядок и значения не искажены")

    check(
        pd.api.types.is_datetime64_any_dtype(result["begin"]),
        f"begin приведён к дате/времени: {result['begin'].dtype}",
    )
    raw = postprocess(merged, "candles", "SBER", _job(parse_dates=False))
    check(not pd.api.types.is_datetime64_any_dtype(raw["begin"]), "без parse_dates время остаётся текстом")

    hi2 = pd.DataFrame(
        {
            "tradedate": ["2024-01-02", "2024-01-03"],
            "tradetime": ["18:40:00", "18:40:00"],
            "metric": ["hhi", "hhi"],
            "reference": [None, ""],
            "systime": ["2024-01-05 12:29:37", "2024-01-03 18:46:48"],
        }
    )
    cleaned = postprocess(hi2, "hi2", "SBER", job)
    check(cleaned["reference"].isna().all(), f"пустые строки -> пропуски: {list(cleaned['reference'])}")
    check(pd.api.types.is_datetime64_any_dtype(cleaned["systime"]), "systime приведён к дате/времени")
    check(cleaned["tradetime"].iloc[0] == "18:40:00", "tradetime оставлен как есть")

    stats = pd.DataFrame(
        {
            "tradedate": ["2024-01-05", "2024-01-06", "2024-01-08"],
            "tradetime": ["10:00:00"] * 3,
            "vol": [1, 2, 3],
        }
    )
    kept = postprocess(stats, "tradestats", "SBER", job)
    check(len(kept) == 3, f"по умолчанию выходные сессии сохраняются: {len(kept)}")
    dropped = postprocess(stats, "tradestats", "SBER", _job(skip_weekends=True))
    check(
        list(dropped["vol"]) == [1, 3],
        f"«пропускать выходные» убирает их и внутри многодневной части: {list(dropped['vol'])}",
    )


def _job(**kwargs) -> ExportJob:
    base = dict(
        api_key="x", market="shares", tickers=["SBER"], datasets=["candles"],
        start=date(2024, 1, 2), end=date(2024, 1, 3), out_dir=Path(tempfile.gettempdir()),
    )
    base.update(kwargs)
    return ExportJob(**base)


def test_calendar_parsing() -> None:
    print("Разбор торгового календаря ISS:")
    start, end = date(2026, 6, 1), date(2026, 6, 30)

    raw = {
        "off_days": {
            "columns": ["tradedate", "is_traded", "reason"],
            "data": [["2026-06-12", 0, "H"], ["2026-06-13", 1, "W"]],
        }
    }
    cal = parse_calendar(raw, "stock", start, end)
    check(cal is not None, "блок с tradedate/is_traded распознан")
    check(cal.is_trading(date(2026, 6, 12)) is False, "праздник 12 июня — неторговый")
    check(cal.is_trading(date(2026, 6, 13)) is True, "суббота с сессией — торговая")
    check(cal.is_trading(date(2026, 6, 14)) is False, "воскресенье без упоминания — неторговое")
    check(cal.is_trading(date(2026, 6, 15)) is True, "понедельник без упоминания — торговый")
    check(cal.is_trading(date(2026, 7, 1)) is None, "день вне календаря — неизвестен")
    check(cal.reasons[date(2026, 6, 13)] == "W", "причина сохранена")

    raw = {
        "calendar": {
            "columns": ["date", "stock_workday", "futures_workday", "currency_workday"],
            "data": [["2026-06-12", 0, 1, 0]],
        }
    }
    check(parse_calendar(raw, "stock", start, end).is_trading(date(2026, 6, 12)) is False, "колонка фондового рынка")
    check(parse_calendar(raw, "futures", start, end).is_trading(date(2026, 6, 12)) is True, "колонка срочного рынка")

    raw = {
        "days": {
            "columns": ["engine", "tradedate", "is_traded"],
            "data": [["stock", "2026-06-12", 0], ["currency", "2026-06-12", 1]],
        }
    }
    check(parse_calendar(raw, "currency", start, end).is_trading(date(2026, 6, 12)) is True, "строки своего рынка")

    check(parse_calendar({"x": {"columns": ["a"], "data": [[1]]}}, "stock", start, end) is None, "непонятный ответ -> None")

    pages = [
        {"off_days": {"columns": ["tradedate", "is_traded"], "data": [["2026-06-12", 0]]}},
        {"off_days": {"columns": ["tradedate", "is_traded"], "data": [["2026-06-13", 1]]}},
    ]
    check(len(merge_pages(pages)["off_days"]["data"]) == 2, "страницы склеиваются")

    cal = TradingCalendar("stock", start, end, {date(2026, 6, 12): False})
    chunks = drop_non_trading(
        [(date(2026, 6, 12), date(2026, 6, 12)), (date(2026, 6, 13), date(2026, 6, 20)),
         (date(2026, 6, 28), date(2026, 7, 2))],
        cal.is_trading,
    )
    check(
        chunks == [(date(2026, 6, 15), date(2026, 6, 19)), (date(2026, 6, 29), date(2026, 7, 2))],
        f"части без торговых дней убраны, края подрезаны, дни вне календаря оставлены: {chunks}",
    )


def test_quality_checks() -> None:
    from moexdata.core.quality import check_frame, cross_check_candles_tradestats

    print("Автоматическая проверка выгрузки:")
    candles = pd.DataFrame(
        {
            "begin": ["2024-01-02 10:00:00", "2024-01-02 10:01:00", "2024-01-02 10:01:00", "2024-01-02 10:02:00"],
            "open": [100.0, 100.0, 100.0, 100.0],
            "high": [101.0, 99.0, 101.0, 112.0],
            "low": [99.0, 98.0, 99.0, 99.0],
            "close": [100.0, 98.5, 100.0, 111.0],
            "volume": [10, 10, 10, -1],
            "value": [1000.0, 985.0, 1000.0, -111.0],
        }
    )
    checks = {i.check: i for i in check_frame(candles, "candles")}
    check("ohlc" in checks and checks["ohlc"].count == 1, f"максимум ниже открытия найден: {checks.get('ohlc')}")
    check("duplicate_time" in checks, "повтор метки времени найден")
    check("negative_volume" in checks, "отрицательный объём найден")
    check("price_jump" in checks and checks["price_jump"].level == "warn", "скачок цены помечен предупреждением")
    clean = candles.drop(index=[1, 2, 3]).reset_index(drop=True)
    check(not [i for i in check_frame(clean, "candles") if i.level != "info"], "чистые данные — без замечаний")

    minutes = pd.DataFrame(
        {
            "begin": [f"2024-01-02 10:0{m}:00" for m in range(5)],
            "high": [101, 102, 101, 103, 101], "low": [99, 98, 99, 99, 97], "volume": [1, 2, 3, 4, 5],
        }
    )
    stats = pd.DataFrame(
        {"tradedate": ["2024-01-02"], "tradetime": ["10:05:00"], "pr_high": [103], "pr_low": [97], "vol": [15]}
    )
    cross = cross_check_candles_tradestats(minutes, stats)
    check(cross and cross[0].level == "info" and "100.0%" in cross[0].message, f"минутки сходятся с TradeStats: {cross}")
    stats.loc[0, "vol"] = 99
    cross = cross_check_candles_tradestats(minutes, stats)
    check(cross[0].level == "warn", "расхождение объёма — предупреждение")


def test_corporate_adjustment() -> None:
    from moexdata.api.corporate import last_buy_date
    from moexdata.core.adjust import apply_factors, corporate_events

    print("Корпоративные события:")
    check(last_buy_date(date(2026, 7, 20)) == date(2026, 7, 17), "T+1: реестр в понедельник -> покупка до пятницы")
    check(last_buy_date(date(2023, 7, 11)) == date(2023, 7, 7), "до 31.07.2023 действовал T+2")

    candles = pd.DataFrame(
        {
            "begin": ["2026-07-17 23:49:00", "2026-07-18 10:00:00", "2026-07-20 10:00:00", "2026-08-03 10:00:00"],
            "open": [280.0, 247.0, 251.0, 125.0],
            "high": [280.0, 248.0, 252.0, 126.0],
            "low": [279.0, 247.0, 250.0, 124.0],
            "close": [280.0, 248.0, 251.0, 125.0],
        }
    )
    dividends = pd.DataFrame(
        {"registryclosedate": ["2026-07-20"], "value": [35.0], "currencyid": ["SUR"], "last_buy_date": ["2026-07-17"]}
    )
    splits = pd.DataFrame({"tradedate": ["2026-08-03"], "before": [1], "after": [2]})
    events = corporate_events(candles, dividends, splits)
    check([e.kind for e in events] == ["dividend", "split"], f"найдены дивиденд и сплит: {[e.kind for e in events]}")
    check(events[0].ex_day == pd.Timestamp("2026-07-18"), f"гэп — в субботнюю сессию: {events[0].ex_day}")
    adjusted = apply_factors(candles, events)
    expected = 280.0 * (1 - 35.0 / 280.0) * 0.5
    check(abs(adjusted["adj_close"].iloc[0] - expected) < 1e-6, f"цена до гэпа скорректирована: {adjusted['adj_close'].iloc[0]}")
    check(adjusted["adj_close"].iloc[2] == 125.5, "после дивиденда — только сплит")
    check(adjusted["adj_close"].iloc[3] == 125.0, "последние цены совпадают с биржевыми")
    check(list(adjusted["close"]) == list(candles["close"]), "исходные цены не тронуты")

    usd = dividends.assign(currencyid="USD")
    check(not [e for e in corporate_events(candles, usd, None) if e.kind == "dividend"], "валютный дивиденд не применяется")


def test_continuous_schedule() -> None:
    from moexdata.core.continuous import Contract, build_schedule, roll_date, select_contracts

    print("Склейка фьючерсов:")
    check(roll_date(date(2024, 3, 21), 5) == date(2024, 3, 14), "переход за 5 рабочих дней до экспирации")
    contracts = [
        Contract("SiM4", date(2024, 6, 20)),
        Contract("SiH4", date(2024, 3, 21)),
        Contract("SiJ4", date(2024, 4, 18)),
        Contract("SiU4", date(2024, 9, 19)),
    ]
    quarterly = select_contracts(contracts, quarterly_only=True)
    check([c.secid for c in quarterly] == ["SiH4", "SiM4", "SiU4"], f"только квартальные: {[c.secid for c in quarterly]}")
    segments = build_schedule(quarterly, date(2024, 3, 1), date(2024, 7, 1), 5)
    check(
        [(s.secid, s.start, s.end) for s in segments] == [
            ("SiH4", date(2024, 3, 1), date(2024, 3, 14)),
            ("SiM4", date(2024, 3, 15), date(2024, 6, 13)),
            ("SiU4", date(2024, 6, 14), date(2024, 7, 1)),
        ],
        f"расписание без пропусков и нахлёстов: {segments}",
    )
    monthly = select_contracts([Contract("BRJ4", date(2024, 4, 1)), Contract("BRK4", date(2024, 5, 2))], True)
    check(len(monthly) == 2, "актив без квартальных контрактов — берутся все")


def test_iss_fallback() -> None:
    from moexdata.api import corporate, iss

    print("Справочники ISS: шлюз пуст -> публичный ISS:")
    calls = []

    def fake_get(path, params, host):
        calls.append(host)
        if host == iss.ALGOPACK:
            return {"dividends": {"columns": ["secid"], "data": []}}
        return {
            "dividends": {
                "columns": ["secid", "isin", "registryclosedate", "value", "currencyid"],
                "data": [["SBER", "RU0009029540", "2025-07-18", 34.84, "SUR"]],
            }
        }

    original = iss.get_json
    iss.get_json = fake_get
    try:
        df = corporate.fetch_dividends("SBER")
    finally:
        iss.get_json = original
    check(calls == [iss.ALGOPACK, iss.PUBLIC], f"сначала шлюз, затем публичный ISS: {calls}")
    check(len(df) == 1 and df["value"].iloc[0] == 34.84, f"дивиденд получен: {df.to_dict('records')}")
    check(str(df["last_buy_date"].iloc[0].date()) == "2025-07-17", "день покупки посчитан")

    from moexdata.errors import ExportError

    def card_or_403(path, params, host):
        if path.startswith("cci/"):
            if host == iss.ALGOPACK:
                raise _http_status(403)
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return {"description": {"columns": ["name", "title", "value"], "data": [["SECID", "Код", "SBER"]]}}

    iss.get_json = card_or_403
    try:
        try:
            corporate.fetch_dividends("SBER")
            check(False, "без источников дивидендов должна быть ошибка")
        except ExportError as err:
            check(err.code == "unsupported", f"код ошибки не фатальный для выгрузки: {err.code}")
            check("подписки" in err.message and "dividends.csv" in err.hint, f"понятная причина и подсказка: {err}")

        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "dividends.csv").write_text(
                "secid;registryclosedate;value\nSBER;2025-07-18;34,84\nGAZP;2025-07-17;0\n", encoding="utf-8"
            )
            corporate.MANUAL_DIRS[:] = [Path(tmp)]
            try:
                manual = corporate.fetch_dividends("SBER")
            finally:
                corporate.MANUAL_DIRS[:] = []
        check(len(manual) == 1 and manual["value"].iloc[0] == 34.84, f"свой файл dividends.csv: {manual.to_dict('records')}")
        check(str(manual["last_buy_date"].iloc[0].date()) == "2025-07-17", "день покупки посчитан и для своего файла")
    finally:
        iss.get_json = original


def _http_status(status: int):
    import httpx

    request = httpx.Request("GET", "https://apim.moex.com/iss/x.json")
    return httpx.HTTPStatusError("boom", request=request, response=httpx.Response(status, request=request))


def test_real_iss_formats() -> None:
    from moexdata.api import corporate, iss

    print("Реальные ответы ISS:")
    raw = {
        "off_days": {
            "columns": ["tradedate", "currency_workday", "currency_trade_session_date", "currency_reason",
                        "futures_workday", "futures_trade_session_date", "futures_reason",
                        "stock_workday", "stock_trade_session_date", "stock_reason"],
            "data": [
                ["2026-10-02", 1, None, None, 1, None, None, 1, None, None],
                ["2026-10-03", 0, None, None, 1, "2026-10-05", "W", 1, "2026-10-05", "W"],
                ["2026-10-04", 0, None, None, 0, None, None, 0, None, None],
            ],
        }
    }
    span = (date(2026, 10, 1), date(2026, 10, 4))
    stock = parse_calendar(raw, "stock", *span)
    check(stock.is_trading(date(2026, 10, 3)) is True, "фондовый рынок: суббота 03.10 торговая")
    check(stock.reasons.get(date(2026, 10, 3)) == "W", "причина берётся из stock_reason")
    check(stock.is_trading(date(2026, 10, 4)) is False, "воскресенье неторговое")
    check(parse_calendar(raw, "currency", *span).is_trading(date(2026, 10, 3)) is False, "валютный рынок в субботу закрыт")

    series = {
        "series": {
            "columns": ["secid", "name", "start_date", "expiration_date", "asset_code", "underlying_asset", "is_traded"],
            "data": [
                ["SiZ6", "Si-12.26", "2025-12-12", "2026-12-17", "Si", "USD000UTSTOM", 1],
                ["SiU6", "Si-9.26", "2025-09-12", "2026-09-17", "Si", "USD000UTSTOM", 0],
                ["SiZ2SiH3", "SiZ2SiH3", "2022-06-09", None, "Si", "USD000UTSTOM", 0],
            ],
        }
    }
    original = iss.fetch
    iss.fetch = lambda path, params=None, block=None: (series, iss.ALGOPACK)
    try:
        contracts = client._expired_series("Si")
    finally:
        iss.fetch = original
    check(
        sorted(c.secid for c in contracts) == ["SiU6", "SiZ6"],
        f"истёкшие контракты по asset_code, спреды без экспирации отброшены: {contracts}",
    )

    card = {"description": {"columns": ["name", "title", "value"], "data": [["SECID", "Код", "SBER"]]}}
    check(corporate.normalize_dividends(card, "SBER").empty, "карточка бумаги не принимается за дивиденды")
    cci = {
        "corp_actions": {
            "columns": ["secid", "record_date", "dividend_value", "currency"],
            "data": [["SBER", "2025-07-18", 34.84, "RUB"], ["GAZP", "2025-07-17", 0.0, "RUB"]],
        }
    }
    divs = corporate.normalize_dividends(cci, "SBER")
    check(len(divs) == 1 and divs["value"].iloc[0] == 34.84, f"формат корпоративных действий распознан: {divs.to_dict('records')}")


def test_moscow_time() -> None:
    print("Биржевое время и периоды:")
    evening = datetime(2024, 3, 5, 23, 30, tzinfo=MOSCOW_TZ)
    check(not is_settled(date(2024, 3, 5), evening), "идущий день не окончательный")
    night = datetime(2024, 3, 6, 1, 0, tzinfo=MOSCOW_TZ)
    check(not is_settled(date(2024, 3, 5), night), "сразу после полуночи метрики ещё досчитываются")
    morning = datetime(2024, 3, 6, 9, 0, tzinfo=MOSCOW_TZ)
    check(is_settled(date(2024, 3, 5), morning), "утром следующего дня день закрыт")
    check(source_period("5min") == "1min", "5-минутки строятся из минуток")
    check(source_period("30min") == "10min", "30-минутки строятся из 10-минуток")
    check(source_period("4W") == "1W", "4 недели строятся из недель")


def test_writer() -> None:
    print("Запись файлов:")
    df = pd.DataFrame({"begin": ["2024-01-02"], "close": [1.5]})
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        name = writer.build_filename(
            "{ticker}_{dataset}{period}_{start}_{end}",
            ticker="SBER", dataset="candles", period="1min",
            start="2024-01-01", end="2024-01-31", market="shares",
        )
        check(name == "SBER_candles_1min_2024-01-01_2024-01-31", f"имя файла: {name}")

        csv_path = writer.write_dataframe(
            df, writer.target_path(root, name, "csv", False), "csv", csv_sep=";"
        )
        check(csv_path.exists() and csv_path.suffix == ".csv", "CSV записан")
        check(";" in csv_path.read_text(encoding="utf-8-sig"), "учтён разделитель CSV")

        gz_path = writer.write_dataframe(
            df, writer.target_path(root, name, "csv", True), "csv", csv_gzip=True
        )
        check(gz_path.name.endswith(".csv.gz"), "CSV сжат в .gz")

        pq_path = writer.write_dataframe(df, writer.target_path(root, name, "parquet", False), "parquet")
        check(pq_path.exists() and len(pd.read_parquet(pq_path)) == 1, "Parquet записан и читается")

        broken = writer.build_filename(
            "{unknown}", ticker="SBER", dataset="candles", period="",
            start="a", end="b", market="shares",
        )
        check(broken == "SBER_candles_a_b", f"битый шаблон -> имя по умолчанию ({broken})")

        check(writer.safe_name('SI<>:"/\\|?*') != "", "недопустимые символы в имени вычищены")


def test_errors() -> None:
    print("Классификация ошибок:")
    import httpx

    request = httpx.Request("GET", "https://apim.moex.com/iss/x.json")

    def http_error(status: int) -> httpx.HTTPStatusError:
        response = httpx.Response(status, request=request)
        return httpx.HTTPStatusError("boom", request=request, response=response)

    check(classify(http_error(403)).code == "forbidden", "403 -> forbidden")
    check(classify(http_error(429)).retryable, "429 помечен как повторяемый")
    check(classify(http_error(503)).retryable, "503 помечен как повторяемый")
    check(not classify(http_error(401)).retryable, "401 не повторяется")
    check(classify(httpx.ConnectError("no route")).code == "network", "сетевая ошибка")
    check(classify(httpx.ReadTimeout("slow")).code == "timeout", "таймаут")
    check(classify(LookupError("Unrecognized ticker' name 'XXX'")).code == "unknown_ticker", "тикер не найден")
    check(classify(NotImplementedError("x")).code == "unsupported", "набор недоступен на рынке")
    check("Непредвиденная" in classify(RuntimeError("?")).message, "неизвестная ошибка описана")


def test_settings() -> None:
    print("Настройки:")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "settings.json"
        s = Settings()
        s.api_key = "secret-key"
        s.tickers = ["SBER", "GAZP"]
        s.candle_period = "1min"
        s.save(path)
        loaded = Settings.load(path)
        check(loaded.api_key == "secret-key", "API-ключ запоминается между запусками")
        check(loaded.tickers == ["SBER", "GAZP"], "выбранные тикеры запоминаются")

        path.write_text('{"file_format": "xlsx", "chunk_days": -5}', encoding="utf-8")
        fixed = Settings.load(path)
        check(fixed.file_format == "parquet", "испорченный формат заменён значением по умолчанию")
        check(fixed.chunk_days == 1, "отрицательный размер части исправлен")

        check(Settings.load(Path(tmp) / "нет.json").api_key == "", "отсутствующий файл -> значения по умолчанию")

        check(not Settings().skip_weekends, "по умолчанию выходные сессии качаются")
        path.write_text('{"skip_weekends": true, "api_key": "k"}', encoding="utf-8")
        migrated = Settings.load(path)
        check(not migrated.skip_weekends, "старый файл настроек: пропуск выходных сброшен")
        check(migrated.api_key == "k", "остальные значения старого файла сохранены")
        migrated.skip_weekends = True
        migrated.save(path)
        check(Settings.load(path).skip_weekends, "явный выбор в новом файле сохраняется")


class _FakeTicker:
    FIRST = date(2007, 1, 15)

    def __init__(self, calls: list):
        self.calls = calls

    def _answer(self, latest: bool) -> pd.DataFrame:
        if latest:
            return pd.DataFrame({"begin": [f"{date.today() - timedelta(days=1)} 23:59:00"]})
        return pd.DataFrame({"begin": [f"{self.FIRST} 10:00:00", "2007-01-16 10:00:00"]})

    def candles(self, start, end, period, latest=False):
        self.calls.append(("candles", start, end, period, latest))
        return self._answer(latest)

    def tradestats(self, start, end, latest=False):
        self.calls.append(("tradestats", start, end, "", latest))
        return pd.DataFrame()

    def futoi(self, start, end):
        self.calls.append(("futoi", start, end, "", False))
        return pd.DataFrame({"tradedate": [str(self.FIRST), "2007-01-16"]})


def test_history_detection() -> None:
    print("Разведка глубины доступной истории:")
    calls: list = []
    original = client.get_ticker
    client.get_ticker = lambda market, secid, api_key: _FakeTicker(calls)
    try:
        span = client.detect_history_range("shares", "SBER", "candles", "key")
        yesterday = date.today() - timedelta(days=1)
        check(span == (_FakeTicker.FIRST, yesterday), f"начало и конец истории свечей: {span}")
        check(
            calls[0][1] == client.HISTORY_FLOOR and calls[0][3] == "1D",
            f"разведка идёт дневными свечами от нижней границы: {calls[0]}",
        )
        check(len(calls) == 2 and calls[1][4] is True, "хватило двух запросов: обычного и latest")

        calls.clear()
        check(
            client.detect_history_range("shares", "SBER", "tradestats", "key") is None,
            "пустой ответ — истории нет, второй запрос не нужен",
        )
        check(len(calls) == 1, f"на пустой набор ушёл один запрос: {len(calls)}")

        calls.clear()
        client.detect_history_range("shares", "SBER", "candles", "key", period="5min")
        check(
            calls[0][3] == "1min",
            f"для 5-минуток разведка идёт по минуткам — их история короче дневной: {calls[0][3]}",
        )

        calls.clear()
        span = client.detect_history_range("futures", "SiZ5", "futoi", "key")
        check(
            span == (_FakeTicker.FIRST, date.today()),
            f"FUTOI не умеет latest — конец истории считается сегодняшним: {span}",
        )
    finally:
        client.get_ticker = original


def main() -> int:
    for test in (
        test_chunking,
        test_auto_chunks,
        test_validation,
        test_postprocess,
        test_writer,
        test_errors,
        test_settings,
        test_moscow_time,
        test_calendar_parsing,
        test_quality_checks,
        test_iss_fallback,
        test_real_iss_formats,
        test_corporate_adjustment,
        test_continuous_schedule,
        test_history_detection,
    ):
        test()
        print()
    if failures:
        print(f"ПРОВАЛЕНО проверок: {len(failures)}")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("Все офлайн-проверки пройдены.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
