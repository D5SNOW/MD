from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from moexdata.api import client
from moexdata.api.datasets import CANDLE_PERIODS, DATASETS, MARKETS
from moexdata.core.exporter import ExportJob, run_export
from moexdata.errors import MoexDataError, classify
from moexdata.settings import Settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="moexdata",
        description="Выгрузка данных MOEX Algopack (свечи, tradestats, orderstats и др.)",
    )
    parser.add_argument("--api-key", default="", help="ключ Algopack (по умолчанию из настроек)")
    parser.add_argument("--market", default=None, choices=sorted(MARKETS), help="рынок")
    parser.add_argument("--tickers", default="", help="список тикеров через запятую")
    parser.add_argument(
        "--datasets", default="candles", help=f"наборы данных через запятую: {', '.join(DATASETS)}"
    )
    parser.add_argument("--start", default=None, help="дата начала ГГГГ-ММ-ДД")
    parser.add_argument("--end", default=None, help="дата окончания ГГГГ-ММ-ДД")
    parser.add_argument(
        "--full-history",
        action="store_true",
        help="выгрузить всю доступную историю инструмента (даты определяются автоматически)",
    )
    parser.add_argument(
        "--period", default=None, choices=[key for key, _, _, _ in CANDLE_PERIODS],
        help="интервал свечей",
    )
    parser.add_argument("--format", dest="file_format", default=None, choices=["csv", "parquet"])
    parser.add_argument("--out", default=None, help="папка для выгрузки")
    parser.add_argument("--chunk-days", type=int, default=None, help="дней на один запрос")
    parser.add_argument("--no-cache", action="store_true", help="не использовать кеш частей")
    weekends = parser.add_mutually_exclusive_group()
    weekends.add_argument(
        "--skip-weekends", dest="skip_weekends", action="store_true", default=None,
        help="не качать субботы и воскресенья",
    )
    weekends.add_argument(
        "--with-weekends", dest="skip_weekends", action="store_false",
        help="качать и выходные сессии (по умолчанию)",
    )
    parser.add_argument(
        "--no-calendar", action="store_true",
        help="не сверяться с торговым календарём MOEX",
    )
    parser.add_argument("--combine", action="store_true", help="один файл на все тикеры")
    parser.add_argument("--no-validate", action="store_true", help="не проверять качество данных")
    parser.add_argument(
        "--update", action="store_true",
        help="догрузить ранее выгруженные файлы до сегодняшнего дня",
    )
    parser.add_argument(
        "--adjust", action="store_true",
        help="акции: добавить цены с поправкой на дивиденды и сплиты (adj_*)",
    )
    parser.add_argument(
        "--continuous", action="store_true",
        help="фьючерсы: склеить контракты актива в непрерывный ряд (тикер — Si или SiZ5)",
    )
    parser.add_argument("--roll-days", type=int, default=None, help="переход за N рабочих дней до экспирации")
    parser.add_argument(
        "--roll-adjust", default=None, choices=["none", "diff", "ratio"],
        help="как убирать гэпы на швах контрактов",
    )
    parser.add_argument("--quarterly", action="store_true", help="склеивать только квартальные контракты")
    parser.add_argument("--verbose", action="store_true", help="печатать каждый запрос")
    parser.add_argument("--list-tickers", action="store_true", help="показать инструменты рынка")
    parser.add_argument("--check-key", action="store_true", help="проверить API-ключ")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.load()
    api_key = args.api_key.strip() or settings.resolved_api_key()
    market = args.market or settings.market

    if args.check_key:
        ok, message = client.check_api_key(api_key)
        print(message)
        return 0 if ok else 2

    if args.list_tickers:
        try:
            df = client.list_tickers(market, api_key)
        except Exception as exc:  # noqa: BLE001
            print(f"Ошибка: {classify(exc)}", file=sys.stderr)
            return 2
        for row in df.itertuples(index=False):
            print(f"{row.secid}\t{row.shortname}")
        return 0

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()] or settings.tickers
    datasets = [d.strip() for d in args.datasets.split(",") if d.strip()]

    job = ExportJob(
        api_key=api_key,
        market=market,
        tickers=tickers,
        datasets=datasets,
        start=date.fromisoformat(args.start or settings.start_date),
        end=date.fromisoformat(args.end or settings.end_date),
        out_dir=Path(args.out or settings.out_dir),
        candle_period=args.period or settings.candle_period,
        file_format=args.file_format or settings.file_format,
        full_history=args.full_history or settings.full_history,
        chunk_mode="manual" if args.chunk_days else settings.chunk_mode,
        chunk_days=args.chunk_days or settings.chunk_days,
        skip_weekends=settings.skip_weekends if args.skip_weekends is None else args.skip_weekends,
        retries=settings.retries,
        retry_pause=settings.retry_pause,
        request_pause=settings.request_pause,
        use_cache=not args.no_cache and settings.use_cache,
        keep_parts=settings.keep_parts,
        dedupe=settings.dedupe,
        sort_rows=settings.sort_rows,
        add_ticker_column=settings.add_ticker_column,
        parse_dates=settings.parse_dates,
        use_calendar=settings.use_calendar and not args.no_calendar,
        validate_data=settings.validate_data and not args.no_validate,
        adjust_prices=args.adjust or settings.adjust_prices,
        update_existing=args.update or settings.update_existing,
        continuous=args.continuous or settings.continuous,
        roll_days=settings.roll_days if args.roll_days is None else args.roll_days,
        roll_adjust=args.roll_adjust or settings.roll_adjust,
        quarterly_only=args.quarterly or settings.quarterly_only,
        combine_tickers=args.combine or settings.combine_tickers,
        subfolder_per_ticker=settings.subfolder_per_ticker,
        write_manifest=settings.write_manifest,
        filename_template=settings.filename_template,
        csv_sep=settings.csv_sep,
        csv_decimal=settings.csv_decimal,
        csv_encoding=settings.csv_encoding,
        csv_gzip=settings.csv_gzip,
        parquet_compression=settings.parquet_compression,
    )

    def log(level: str, message: str) -> None:
        if level == "debug" and not args.verbose:
            return
        print(message)

    try:
        summary = run_export(job, log_cb=log)
    except MoexDataError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"Ошибка: {classify(exc)}", file=sys.stderr)
        return 2
    return 1 if summary.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
