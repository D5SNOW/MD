from __future__ import annotations

import sys
import tempfile
import tkinter as tk
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from moexdata import settings as settings_module  # noqa: E402
from moexdata.api import client  # noqa: E402

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"  ok   {message}")
    else:
        failures.append(message)
        print(f"  FAIL {message}")


FAKE_TICKERS = {
    "shares": pd.DataFrame({"secid": ["GAZP", "LKOH", "SBER"], "shortname": ["Газпром", "Лукойл", "Сбербанк"]}),
    "futures": pd.DataFrame({"secid": ["SiZ5", "RIZ5"], "shortname": ["Si-12.25", "RTS-12.25"]}),
    "currency": pd.DataFrame({"secid": ["CNYRUB_TOM"], "shortname": ["CNYRUB"]}),
    "index": pd.DataFrame({"secid": ["IMOEX"], "shortname": ["Индекс МосБиржи"]}),
    "bonds": pd.DataFrame({"secid": ["SU26238RMFS4"], "shortname": ["ОФЗ"]}),
    "options": pd.DataFrame({"secid": ["SI95000BC5"], "shortname": ["Опцион"]}),
}


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="moexdata_gui_"))
    settings_module.CONFIG_PATH = tmp / "settings.json"
    client.list_tickers = lambda market, api_key: FAKE_TICKERS[market]

    from moexdata.gui.app import MoexDataApp

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"Tk недоступен в этой среде: {exc}")
        return 0
    root.withdraw()

    print("Сборка окна:")
    app = MoexDataApp(root)
    root.update()
    check(True, "окно и все вкладки собраны без ошибок")
    check(len(app.notebook.tabs()) == 5, "пять вкладок на месте")

    print("\nБез ключа список инструментов не запрашивается:")
    app._load_tickers_async()
    root.update()
    check(not app.all_tickers, "без API-ключа запрос не отправляется")
    check("API-ключ" in app.status_var.get(), f"подсказка в статусе: {app.status_var.get()}")

    print("\nЗагрузка тикеров:")
    app.api_key_var.set("test-key")
    app._load_tickers_async()
    for _ in range(50):
        root.update()
        if app.all_tickers:
            break
        root.after(20)
    check(len(app.all_tickers) == 3, f"список инструментов заполнен: {len(app.all_tickers)}")

    print("\nПоиск и выбор:")
    app.search_var.set("сбер")
    root.update()
    check(app.visible_secids == ["SBER"], f"поиск по названию работает: {app.visible_secids}")
    app._select_all_visible()
    check(app.selected_secids == {"SBER"}, "тикер выбран")
    app.search_var.set("")
    root.update()
    app.ticker_list.selection_set(0)
    app._on_listbox_select()
    check(app.selected_secids == {"GAZP", "SBER"}, f"выбор сохраняется при фильтрации: {app.selected_secids}")

    print("\nПлан разбиения запросов:")
    app.start_var.set("2024-03-01")
    app.end_var.set("2024-03-15")
    app.period_label_var.set("1 минута")
    app._update_plan()
    root.update()
    check("30 запрос" in app.plan_var.get(), f"план на 2 тикера x 15 дней с выходными: {app.plan_var.get()}")
    app.skip_weekends_var.set(True)
    app._update_plan()
    root.update()
    check("22 запрос" in app.plan_var.get(), f"без выходных — 2 тикера x 11 дней: {app.plan_var.get()}")
    app.skip_weekends_var.set(False)
    app.period_label_var.set("1 день")
    app._update_plan()
    check("2 запрос(ов)" in app.plan_var.get(), f"дневные свечи — один запрос на тикер: {app.plan_var.get()}")
    app.period_label_var.set("1 минута")
    app._update_plan()

    print("\nСмена рынка:")
    app.market_label_var.set("Фьючерсы (RFUD)")
    app._on_market_change(load_tickers=False)
    root.update()
    check("futoi" in app.dataset_vars, "на фьючерсах появился набор FUTOI")
    check(app.selected_secids == set(), "выбор тикеров сброшен при смене рынка")
    app.market_label_var.set("Индексы (SNDX)")
    app._on_market_change(load_tickers=False)
    check(list(app.dataset_vars) == ["candles"], f"для индексов доступны только свечи: {list(app.dataset_vars)}")
    app.market_label_var.set("Акции (TQBR)")
    app._on_market_change(load_tickers=False)

    print("\nБыстрый выбор периода:")
    app.quick_var.set("Последние 30 дней")
    app._apply_quick_range()
    dates = app._parse_dates()
    check(dates is not None and (dates[1] - dates[0]).days == 30, "быстрый выбор проставил даты")

    print("\nВся доступная история:")
    app.full_history_var.set(True)
    app._on_full_history_toggle()
    root.update()
    check(str(app.start_entry["state"]) == "disabled", "поля дат отключены")
    check("вся доступная история" in app.plan_var.get().lower(), f"план: {app.plan_var.get()}")
    check(app._build_job().full_history, "флаг попал в задание")

    print("\nПроверка, догрузка, корректировки:")
    app.update_var.set(True)
    app.adjust_var.set(True)
    app.roll_adjust_var.set("умножение на отношение")
    job = app._build_job()
    check(job.validate_data and job.update_existing and job.adjust_prices, "флаги попали в задание")
    check(job.roll_adjust == "ratio", f"способ корректировки швов: {job.roll_adjust}")
    app.continuous_var.set(True)
    check(not app._build_job().continuous, "склейка не включается для акций")
    app.update_var.set(False)
    app.adjust_var.set(False)
    app.continuous_var.set(False)
    app.roll_adjust_var.set("сдвиг на разницу цен")
    app.full_history_var.set(False)
    app._on_full_history_toggle()
    root.update()
    check(str(app.start_entry["state"]) == "normal", "поля дат снова активны")

    print("\nСохранение настроек:")
    app.api_key_var.set("my-secret-key")
    app.out_dir_var.set(str(tmp / "out"))
    app.format_var.set("csv")
    app.csv_sep_var.set(";")
    app._save_settings_clicked()
    reloaded = settings_module.Settings.load(settings_module.CONFIG_PATH)
    check(reloaded.api_key == "my-secret-key", "API-ключ сохранён в файл настроек")
    check(reloaded.csv_sep == ";", "дополнительные параметры сохранены")
    check(reloaded.file_format == "csv", "формат сохранён")

    print("\nОтчёт об ошибках:")
    from moexdata.core.exporter import ExportResult, ExportSummary

    summary = ExportSummary(started_at="x", finished_at="y")
    summary.results = [
        ExportResult(
            "SBER", "candles", "ok", 5400, 11, 0, str(tmp / "SBER.parquet"),
            start="2024-03-01", end="2024-03-15",
        ),
        ExportResult(
            "GAZP", "tradestats", "error", 0, 0, 3, None, "forbidden", "Доступ запрещён (403).",
        ),
        ExportResult("LKOH", "candles", "empty", 0, 5, 0, None, "", "Нет данных за выбранный период"),
    ]
    app._fill_errors_table(summary)
    app.last_summary = summary
    rows = app.errors_tree.get_children()
    columns = list(app.errors_tree["columns"])
    check(len(rows) == 3, "таблица ошибок заполнена")
    check(
        app.errors_tree.item(rows[1])["values"][columns.index("code")] == "forbidden",
        "код ошибки показан",
    )
    check(
        app.errors_tree.item(rows[0])["values"][columns.index("period")] == "2024-03-01 — 2024-03-15",
        "выгруженный диапазон показан",
    )
    check(summary.failed_pairs() == {("GAZP", "tradestats")}, "повтор нацелен только на неудачные пары")

    print("\nСборка задания из формы:")
    app.selected_secids = {"SBER"}
    app.start_var.set("2024-03-01")
    app.end_var.set("2024-03-15")
    job = app._build_job()
    job.validate()
    check(job.tickers == ["SBER"] and job.candle_period == "1min", "задание собрано корректно")
    check(job.chunk_days_for("candles") == 1, "минутные свечи режутся по дням")

    app.start_var.set("не дата")
    try:
        app._build_job()
        check(False, "некорректная дата должна отвергаться")
    except Exception as exc:  # noqa: BLE001
        check("ГГГГ-ММ-ДД" in str(exc), f"понятное сообщение о дате: {exc}")

    root.destroy()
    print()
    if failures:
        print(f"ПРОВАЛЕНО проверок: {len(failures)}")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("Все проверки интерфейса пройдены.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
