from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from datetime import date, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from moexdata.api import client
from moexdata.api.datasets import (
    CANDLE_PERIODS,
    DATASETS,
    MARKETS,
    PERIOD_LABELS,
    datasets_for_market,
)
from moexdata.core.chunking import split_range
from moexdata.core.exporter import ExportJob, ExportSummary, run_export
from moexdata.errors import MoexDataError, classify
from moexdata.settings import CONFIG_PATH, DEFAULT_EXPORT_DIR, Settings
from moexdata.timeutil import moscow_today

ROLL_ADJUST_LABELS = {
    "diff": "сдвиг на разницу цен",
    "ratio": "умножение на отношение",
    "none": "не убирать",
}

QUICK_RANGES: dict[str, int | None] = {
    "Последние 7 дней": 7,
    "Последние 30 дней": 30,
    "Последние 90 дней": 90,
    "Последний год": 365,
    "Последние 3 года": 365 * 3,
    "С начала года": None,
}

LEVEL_COLORS = {
    "ok": "#0a7d33",
    "error": "#b00020",
    "warn": "#a06000",
    "debug": "#666666",
    "info": "#000000",
}

STATUS_LABELS = {"ok": "успешно", "empty": "нет данных", "error": "ошибка"}


class MoexDataApp(ttk.Frame):
    def __init__(self, master: tk.Tk):
        super().__init__(master, padding=8)
        self.master = master
        self.settings = Settings.load()

        master.title("MoexData — выгрузка данных MOEX Algopack")
        master.geometry("1120x780")
        master.minsize(940, 660)
        master.rowconfigure(0, weight=1)
        master.columnconfigure(0, weight=1)
        self.grid(row=0, column=0, sticky="nsew")

        self.queue: queue.Queue = queue.Queue()
        self.all_tickers: list[tuple[str, str]] = []
        self.visible_secids: list[str] = []
        self.selected_secids: set[str] = set(self.settings.tickers)
        self.dataset_vars: dict[str, tk.BooleanVar] = {}
        self.stop_flag = False
        self.worker: threading.Thread | None = None
        self.last_summary: ExportSummary | None = None
        self.retry_pairs: set[tuple[str, str]] | None = None
        self._ui_ready = False
        self._current_market = self.settings.market

        self._build_ui()
        self._ui_ready = True
        self._on_full_history_toggle()
        self._on_market_change(load_tickers=True)
        master.protocol("WM_DELETE_WINDOW", self._on_close)
        master.after(100, self._poll_queue)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        self._build_settings_bar()

        self.notebook = ttk.Notebook(self)
        self.notebook.grid(row=1, column=0, sticky="nsew", pady=(8, 0))

        self.tab_main = ttk.Frame(self.notebook, padding=8)
        self.tab_extra = ttk.Frame(self.notebook, padding=8)
        self.tab_log = ttk.Frame(self.notebook, padding=8)
        self.tab_errors = ttk.Frame(self.notebook, padding=8)
        self.tab_help = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(self.tab_main, text="Импорт данных")
        self.notebook.add(self.tab_extra, text="Дополнительно")
        self.notebook.add(self.tab_log, text="Журнал")
        self.notebook.add(self.tab_errors, text="Ошибки")
        self.notebook.add(self.tab_help, text="Справка")

        self._build_main_tab()
        self._build_extra_tab()
        self._build_log_tab()
        self._build_errors_tab()
        self._build_help_tab()
        self._build_run_bar()

    def _build_settings_bar(self) -> None:
        frame = ttk.LabelFrame(self, text="Настройки доступа к MOEX Algopack", padding=8)
        frame.grid(row=0, column=0, sticky="ew")
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="API-ключ:").grid(row=0, column=0, sticky="w")
        self.api_key_var = tk.StringVar(value=self.settings.api_key)
        self.api_entry = ttk.Entry(frame, textvariable=self.api_key_var, show="•")
        self.api_entry.grid(row=0, column=1, sticky="ew", padx=6)

        self.show_key_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            frame, text="Показать", variable=self.show_key_var, command=self._toggle_key_visibility
        ).grid(row=0, column=2, padx=(0, 6))
        ttk.Button(frame, text="Сохранить", command=self._save_settings_clicked).grid(row=0, column=3)
        ttk.Button(frame, text="Проверить ключ", command=self._check_key_clicked).grid(
            row=0, column=4, padx=6
        )

        self.key_status_var = tk.StringVar(
            value="Ключ сохранён." if self.settings.api_key else "Ключ не задан."
        )
        self.key_status = ttk.Label(frame, textvariable=self.key_status_var, foreground="#555")
        self.key_status.grid(row=1, column=1, columnspan=4, sticky="w", pady=(4, 0))
        ttk.Label(frame, text=f"Файл настроек: {CONFIG_PATH}", foreground="#888").grid(
            row=2, column=0, columnspan=5, sticky="w", pady=(2, 0)
        )

    def _toggle_key_visibility(self) -> None:
        self.api_entry.configure(show="" if self.show_key_var.get() else "•")

    def _build_main_tab(self) -> None:
        tab = self.tab_main
        tab.columnconfigure(0, weight=3, uniform="cols")
        tab.columnconfigure(1, weight=4, uniform="cols")
        tab.rowconfigure(0, weight=1)

        left = ttk.LabelFrame(tab, text="Рынок и тикеры", padding=8)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(3, weight=1)

        market_row = ttk.Frame(left)
        market_row.grid(row=0, column=0, sticky="ew")
        ttk.Label(market_row, text="Рынок:").pack(side="left")
        self.market_label_var = tk.StringVar(value=MARKETS[self.settings.market].label)
        market_combo = ttk.Combobox(
            market_row,
            textvariable=self.market_label_var,
            state="readonly",
            values=[info.label for info in MARKETS.values()],
            width=20,
        )
        market_combo.pack(side="left", padx=6)
        market_combo.bind("<<ComboboxSelected>>", lambda e: self._on_market_change(load_tickers=True))
        ttk.Button(market_row, text="Обновить", command=self._load_tickers_async).pack(side="right")

        search_row = ttk.Frame(left)
        search_row.grid(row=1, column=0, sticky="ew", pady=(6, 4))
        ttk.Label(search_row, text="Поиск:").pack(side="left")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *a: self._apply_filter())
        ttk.Entry(search_row, textvariable=self.search_var).pack(
            side="left", fill="x", expand=True, padx=6
        )

        sel_row = ttk.Frame(left)
        sel_row.grid(row=2, column=0, sticky="ew", pady=(0, 4))
        ttk.Button(sel_row, text="Выбрать все", command=self._select_all_visible).pack(side="left")
        ttk.Button(sel_row, text="Снять выбор", command=self._clear_selection).pack(side="left", padx=6)
        self.selected_count_var = tk.StringVar(value="Выбрано: 0")
        ttk.Label(sel_row, textvariable=self.selected_count_var).pack(side="right")

        list_frame = ttk.Frame(left)
        list_frame.grid(row=3, column=0, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        self.ticker_list = tk.Listbox(list_frame, selectmode=tk.EXTENDED, exportselection=False)
        self.ticker_list.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Scrollbar(list_frame, orient="vertical", command=self.ticker_list.yview)
        bar.grid(row=0, column=1, sticky="ns")
        self.ticker_list.configure(yscrollcommand=bar.set)
        self.ticker_list.bind("<<ListboxSelect>>", lambda e: self._on_listbox_select())

        right = ttk.Frame(tab)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)

        self._build_period_frame(right)
        self._build_datasets_frame(right)
        self._build_output_frame(right)

    def _build_period_frame(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Период и интервал", padding=8)
        frame.grid(row=0, column=0, sticky="ew")
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(3, weight=1)

        ttk.Label(frame, text="Быстрый выбор:").grid(row=0, column=0, sticky="w")
        self.quick_var = tk.StringVar()
        self.quick_combo = ttk.Combobox(
            frame, textvariable=self.quick_var, state="readonly",
            values=list(QUICK_RANGES.keys()), width=22,
        )
        self.quick_combo.grid(row=0, column=1, sticky="w", padx=6)
        self.quick_combo.bind("<<ComboboxSelected>>", lambda e: self._apply_quick_range())

        ttk.Label(frame, text="С (ГГГГ-ММ-ДД):").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.start_var = tk.StringVar(value=self.settings.start_date)
        self.start_var.trace_add("write", lambda *a: self._update_plan())
        self.start_entry = ttk.Entry(frame, textvariable=self.start_var, width=14)
        self.start_entry.grid(row=1, column=1, sticky="w", padx=6, pady=(6, 0))

        ttk.Label(frame, text="По (ГГГГ-ММ-ДД):").grid(row=1, column=2, sticky="w", pady=(6, 0))
        self.end_var = tk.StringVar(value=self.settings.end_date)
        self.end_var.trace_add("write", lambda *a: self._update_plan())
        self.end_entry = ttk.Entry(frame, textvariable=self.end_var, width=14)
        self.end_entry.grid(row=1, column=3, sticky="w", padx=6, pady=(6, 0))

        self.full_history_var = tk.BooleanVar(value=self.settings.full_history)
        self.full_history_check = ttk.Checkbutton(
            frame,
            text="Вся доступная история инструмента (даты определяются автоматически)",
            variable=self.full_history_var,
            command=self._on_full_history_toggle,
        )
        self.full_history_check.grid(row=6, column=0, columnspan=4, sticky="w", pady=(6, 0))

        ttk.Label(frame, text="Интервал (датафрейм):").grid(row=2, column=0, sticky="w", pady=(6, 0))
        current = next(
            (label for key, label, _, _ in CANDLE_PERIODS if key == self.settings.candle_period),
            "1 минута",
        )
        self.period_label_var = tk.StringVar(value=current)
        period_combo = ttk.Combobox(
            frame, textvariable=self.period_label_var, state="readonly",
            values=[label for _, label, _, _ in CANDLE_PERIODS], width=14,
        )
        period_combo.grid(row=2, column=1, sticky="w", padx=6, pady=(6, 0))
        period_combo.bind("<<ComboboxSelected>>", lambda e: self._update_plan())

        ttk.Label(frame, text="Разбиение запросов:").grid(row=3, column=0, sticky="w", pady=(6, 0))
        chunk_row = ttk.Frame(frame)
        chunk_row.grid(row=3, column=1, columnspan=3, sticky="w", padx=6, pady=(6, 0))
        self.chunk_mode_var = tk.StringVar(value=self.settings.chunk_mode)
        ttk.Radiobutton(
            chunk_row, text="Авто", variable=self.chunk_mode_var, value="auto",
            command=self._update_plan,
        ).pack(side="left")
        ttk.Radiobutton(
            chunk_row, text="Вручную:", variable=self.chunk_mode_var, value="manual",
            command=self._update_plan,
        ).pack(side="left", padx=(10, 4))
        self.chunk_days_var = tk.StringVar(value=str(self.settings.chunk_days))
        self.chunk_days_var.trace_add("write", lambda *a: self._update_plan())
        ttk.Spinbox(
            chunk_row, from_=1, to=3650, width=6, textvariable=self.chunk_days_var
        ).pack(side="left")
        ttk.Label(chunk_row, text="дн. на запрос").pack(side="left", padx=(4, 0))

        self.skip_weekends_var = tk.BooleanVar(value=self.settings.skip_weekends)
        ttk.Checkbutton(
            frame,
            text="Пропускать выходные дни (с 2025 г. на MOEX есть торги по выходным)",
            variable=self.skip_weekends_var,
            command=self._update_plan,
        ).grid(row=4, column=0, columnspan=4, sticky="w", pady=(6, 0))

        self.plan_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.plan_var, foreground="#0a4d8c", wraplength=520).grid(
            row=5, column=0, columnspan=4, sticky="w", pady=(6, 0)
        )

    def _build_datasets_frame(self, parent: ttk.Frame) -> None:
        self.datasets_frame = ttk.LabelFrame(parent, text="Типы данных", padding=8)
        self.datasets_frame.grid(row=1, column=0, sticky="nsew", pady=(8, 0))

    def _build_output_frame(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Куда сохранить", padding=8)
        frame.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="Папка:").grid(row=0, column=0, sticky="w")
        self.out_dir_var = tk.StringVar(value=self.settings.out_dir)
        ttk.Entry(frame, textvariable=self.out_dir_var).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(frame, text="Обзор…", command=self._choose_folder).grid(row=0, column=2)
        ttk.Button(frame, text="Открыть", command=self._open_folder).grid(row=0, column=3, padx=(6, 0))

        ttk.Label(frame, text="Формат:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        fmt_row = ttk.Frame(frame)
        fmt_row.grid(row=1, column=1, columnspan=3, sticky="w", padx=6, pady=(6, 0))
        self.format_var = tk.StringVar(value=self.settings.file_format)
        ttk.Radiobutton(fmt_row, text="Parquet", variable=self.format_var, value="parquet").pack(side="left")
        ttk.Radiobutton(fmt_row, text="CSV", variable=self.format_var, value="csv").pack(side="left", padx=12)

    def _build_extra_tab(self) -> None:
        tab = self.tab_extra
        tab.columnconfigure(0, weight=1, uniform="x")
        tab.columnconfigure(1, weight=1, uniform="x")

        merge = ttk.LabelFrame(tab, text="Склейка и постобработка", padding=8)
        merge.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.dedupe_var = tk.BooleanVar(value=self.settings.dedupe)
        self.sort_var = tk.BooleanVar(value=self.settings.sort_rows)
        self.add_ticker_var = tk.BooleanVar(value=self.settings.add_ticker_column)
        self.parse_dates_var = tk.BooleanVar(value=self.settings.parse_dates)
        self.calendar_var = tk.BooleanVar(value=self.settings.use_calendar)
        self.combine_var = tk.BooleanVar(value=self.settings.combine_tickers)
        self.subfolder_var = tk.BooleanVar(value=self.settings.subfolder_per_ticker)
        self.keep_parts_var = tk.BooleanVar(value=self.settings.keep_parts)
        self.manifest_var = tk.BooleanVar(value=self.settings.write_manifest)
        for row, (text, var) in enumerate(
            [
                ("Удалять дубликаты строк на стыках частей", self.dedupe_var),
                ("Сортировать строки по времени", self.sort_var),
                ("Добавлять колонку с тикером", self.add_ticker_var),
                ("Колонки дат и времени — как дата/время, а не текст", self.parse_dates_var),
                ("Сверять с торговым календарём MOEX (без запросов за праздники, поиск пропусков)",
                 self.calendar_var),
                ("Объединять все тикеры в один файл", self.combine_var),
                ("Складывать файлы в подпапку по тикеру", self.subfolder_var),
                ("Сохранять также по-дневные части (parts/)", self.keep_parts_var),
                ("Сохранять JSON-отчёт о выгрузке", self.manifest_var),
            ]
        ):
            ttk.Checkbutton(merge, text=text, variable=var).grid(row=row, column=0, sticky="w")

        ttk.Label(merge, text="Шаблон имени файла:").grid(row=10, column=0, sticky="w", pady=(8, 0))
        self.template_var = tk.StringVar(value=self.settings.filename_template)
        ttk.Entry(merge, textvariable=self.template_var).grid(row=11, column=0, sticky="ew")
        ttk.Label(
            merge,
            text="Доступно: {ticker} {dataset} {period} {start} {end} {market}",
            foreground="#666",
        ).grid(row=12, column=0, sticky="w")
        merge.columnconfigure(0, weight=1)

        proc = ttk.LabelFrame(tab, text="Проверка, догрузка и корректировки", padding=8)
        proc.grid(row=2, column=0, sticky="nsew", padx=(0, 8), pady=(8, 0))
        self.validate_var = tk.BooleanVar(value=self.settings.validate_data)
        self.update_var = tk.BooleanVar(value=self.settings.update_existing)
        self.adjust_var = tk.BooleanVar(value=self.settings.adjust_prices)
        for row, (text, var) in enumerate(
            [
                ("Проверять качество данных (OHLC, дубли, скачки, сверка свечей с TradeStats)",
                 self.validate_var),
                ("Догружать ранее выгруженные файлы до сегодняшнего дня", self.update_var),
                ("Акции: добавить цены с поправкой на дивиденды и сплиты (adj_*)", self.adjust_var),
            ]
        ):
            ttk.Checkbutton(proc, text=text, variable=var).grid(row=row, column=0, sticky="w")

        fut = ttk.LabelFrame(tab, text="Фьючерсы: непрерывный ряд", padding=8)
        fut.grid(row=2, column=1, sticky="nsew", pady=(8, 0))
        self.continuous_var = tk.BooleanVar(value=self.settings.continuous)
        ttk.Checkbutton(
            fut, text="Склеивать контракты актива (тикер — Si или любой контракт SiZ5)",
            variable=self.continuous_var,
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(fut, text="Переход за рабочих дней до экспирации:").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.roll_days_var = tk.StringVar(value=str(self.settings.roll_days))
        ttk.Spinbox(fut, from_=0, to=30, width=6, textvariable=self.roll_days_var).grid(
            row=1, column=1, sticky="w", padx=6, pady=(4, 0)
        )
        ttk.Label(fut, text="Гэпы на швах:").grid(row=2, column=0, sticky="w", pady=(4, 0))
        self.roll_adjust_var = tk.StringVar(value=ROLL_ADJUST_LABELS[self.settings.roll_adjust])
        ttk.Combobox(
            fut, textvariable=self.roll_adjust_var, state="readonly",
            values=list(ROLL_ADJUST_LABELS.values()), width=22,
        ).grid(row=2, column=1, sticky="w", padx=6, pady=(4, 0))
        self.quarterly_var = tk.BooleanVar(value=self.settings.quarterly_only)
        ttk.Checkbutton(
            fut, text="Только квартальные контракты (мар/июн/сен/дек)", variable=self.quarterly_var,
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))

        net = ttk.LabelFrame(tab, text="Сеть и устойчивость", padding=8)
        net.grid(row=0, column=1, sticky="nsew")
        net.columnconfigure(1, weight=1)
        ttk.Label(net, text="Повторов при сбое:").grid(row=0, column=0, sticky="w")
        self.retries_var = tk.StringVar(value=str(self.settings.retries))
        ttk.Spinbox(net, from_=0, to=10, width=6, textvariable=self.retries_var).grid(
            row=0, column=1, sticky="w", padx=6
        )
        ttk.Label(net, text="Пауза перед повтором, с:").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.retry_pause_var = tk.StringVar(value=str(self.settings.retry_pause))
        ttk.Spinbox(net, from_=0, to=60, increment=0.5, width=6, textvariable=self.retry_pause_var).grid(
            row=1, column=1, sticky="w", padx=6, pady=(4, 0)
        )
        ttk.Label(net, text="Пауза между запросами, с:").grid(row=2, column=0, sticky="w", pady=(4, 0))
        self.request_pause_var = tk.StringVar(value=str(self.settings.request_pause))
        ttk.Spinbox(
            net, from_=0, to=10, increment=0.1, width=6, textvariable=self.request_pause_var
        ).grid(row=2, column=1, sticky="w", padx=6, pady=(4, 0))
        self.cache_var = tk.BooleanVar(value=self.settings.use_cache)
        ttk.Checkbutton(
            net,
            text="Кешировать части (продолжать прерванную выгрузку)",
            variable=self.cache_var,
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(net, text="Очистить кеш", command=self._clear_cache).grid(
            row=4, column=0, sticky="w", pady=(4, 0)
        )

        fmt = ttk.LabelFrame(tab, text="Параметры файлов", padding=8)
        fmt.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        fmt.columnconfigure(4, weight=1)

        ttk.Label(fmt, text="CSV: разделитель").grid(row=0, column=0, sticky="w")
        self.csv_sep_var = tk.StringVar(value=self.settings.csv_sep)
        ttk.Combobox(
            fmt, textvariable=self.csv_sep_var, values=[",", ";", "\t", "|"], width=6
        ).grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(fmt, text="десятичный знак").grid(row=0, column=2, sticky="w")
        self.csv_decimal_var = tk.StringVar(value=self.settings.csv_decimal)
        ttk.Combobox(fmt, textvariable=self.csv_decimal_var, values=[".", ","], width=6).grid(
            row=0, column=3, sticky="w", padx=6
        )
        ttk.Label(fmt, text="кодировка").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.csv_encoding_var = tk.StringVar(value=self.settings.csv_encoding)
        ttk.Combobox(
            fmt, textvariable=self.csv_encoding_var,
            values=["utf-8-sig", "utf-8", "cp1251"], width=12,
        ).grid(row=1, column=1, sticky="w", padx=6, pady=(4, 0))
        self.csv_gzip_var = tk.BooleanVar(value=self.settings.csv_gzip)
        ttk.Checkbutton(fmt, text="сжимать в .csv.gz", variable=self.csv_gzip_var).grid(
            row=1, column=2, columnspan=2, sticky="w", pady=(4, 0)
        )

        ttk.Label(fmt, text="Parquet: сжатие").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.parquet_compression_var = tk.StringVar(value=self.settings.parquet_compression)
        ttk.Combobox(
            fmt, textvariable=self.parquet_compression_var, state="readonly",
            values=["snappy", "gzip", "brotli", "zstd", "none"], width=12,
        ).grid(row=2, column=1, sticky="w", padx=6, pady=(8, 0))

    def _build_log_tab(self) -> None:
        tab = self.tab_log
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)

        bar = ttk.Frame(tab)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ttk.Button(bar, text="Сохранить журнал…", command=self._save_log).pack(side="left")
        ttk.Button(bar, text="Очистить", command=self._clear_log).pack(side="left", padx=6)
        self.verbose_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Подробный вывод (каждый запрос)", variable=self.verbose_var).pack(
            side="left", padx=12
        )

        self.log_text = tk.Text(tab, wrap="word", state="disabled", height=20)
        self.log_text.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(tab, orient="vertical", command=self.log_text.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set)
        for level, color in LEVEL_COLORS.items():
            self.log_text.tag_configure(level, foreground=color)

    def _build_errors_tab(self) -> None:
        tab = self.tab_errors
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)

        bar = ttk.Frame(tab)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ttk.Button(bar, text="Повторить неудачные", command=self._retry_failed).pack(side="left")
        ttk.Button(bar, text="Сохранить отчёт…", command=self._save_error_report).pack(side="left", padx=6)
        self.errors_summary_var = tk.StringVar(value="Выгрузка ещё не выполнялась.")
        ttk.Label(bar, textvariable=self.errors_summary_var).pack(side="right")

        columns = ("ticker", "dataset", "status", "period", "rows", "code", "message")
        self.errors_tree = ttk.Treeview(tab, columns=columns, show="headings", height=16)
        for column, title, width in (
            ("ticker", "Тикер", 90),
            ("dataset", "Набор данных", 150),
            ("status", "Статус", 90),
            ("period", "Диапазон", 170),
            ("rows", "Строк", 70),
            ("code", "Код", 110),
            ("message", "Сообщение", 420),
        ):
            self.errors_tree.heading(column, text=title)
            self.errors_tree.column(column, width=width, anchor="w")
        self.errors_tree.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(tab, orient="vertical", command=self.errors_tree.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.errors_tree.configure(yscrollcommand=scroll.set)
        self.errors_tree.tag_configure("error", foreground=LEVEL_COLORS["error"])
        self.errors_tree.tag_configure("empty", foreground=LEVEL_COLORS["warn"])
        self.errors_tree.tag_configure("ok", foreground=LEVEL_COLORS["ok"])

    def _build_help_tab(self) -> None:
        tab = self.tab_help
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=1)
        text = tk.Text(tab, wrap="word")
        text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(tab, orient="vertical", command=text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        text.configure(yscrollcommand=scroll.set)
        text.insert("1.0", self._help_text())
        text.configure(state="disabled")

    def _help_text(self) -> str:
        lines = [
            "MoexData — выгрузка данных MOEX Algopack",
            "",
            "1. Введите API-ключ Algopack вверху окна и нажмите «Сохранить».",
            "   Ключ хранится в файле настроек и подставляется при следующем запуске.",
            "   Также распознаются переменные окружения MOEXALGO_APIKEY и ALGOPACK_APIKEY.",
            "",
            "2. Выберите рынок, тикеры (можно несколько), типы данных, интервал и даты.",
            "   Галочка «Вся доступная история инструмента» отменяет поля дат: перед",
            "   выгрузкой приложение само спрашивает у биржи, с какой даты по какую есть",
            "   данные, отдельно для каждой пары «тикер × набор». Для одного и того же",
            "   инструмента свечи обычно уходят вглубь на годы дальше, чем метрики",
            "   Algopack, и каждый набор скачается на свою глубину.",
            "",
            "3. Ограничение API: один запрос отдаёт не более 10 000 строк.",
            "   Поэтому диапазон дат режется на части: для минутных свечей — по одному",
            "   дню на запрос, для 5-минутных агрегатов (TradeStats и др.) — по 30 дней.",
            "   Части скачиваются по очереди и склеиваются в один итоговый файл.",
            "   Размер части можно задать вручную в блоке «Разбиение запросов».",
            "",
            "4. Форматы выгрузки: Parquet (компактнее и быстрее) и CSV.",
            "",
            "5. Вкладка «Дополнительно»:",
            "   догрузка ранее выгруженных файлов до сегодняшнего дня;",
            "   проверка качества данных и сверка свечей с TradeStats;",
            "   сверка с торговым календарём MOEX и поиск пропущенных дней;",
            "   цены акций с поправкой на сплиты и дивиденды (дивиденды — из dividends.csv);",
            "   склейка фьючерсных контрактов в непрерывный ряд.",
            "",
            "Наборы данных:",
        ]
        for dataset in DATASETS.values():
            markets = ", ".join(MARKETS[m].label for m in dataset.markets)
            lines.append(f"  • {dataset.label} — {dataset.description}")
            lines.append(f"    Доступен: {markets}")
        lines += [
            "",
            "Возможные ошибки и что делать:",
            "  • 401/403 — ключ не принят или нет подписки на набор данных.",
            "  • 429 — слишком много запросов: увеличьте паузу между запросами.",
            "  • 5xx / таймаут — сбой на стороне MOEX, помогает повтор (настраивается).",
            "  • «Нет данных за период» — в выбранные даты инструмент не торговался.",
            "  • «Достигнут лимит API» в журнале — уменьшите размер части выгрузки.",
            "",
            "Прерванную выгрузку можно продолжить: скачанные части лежат в кеше",
            "(.moexdata_cache в папке выгрузки) и при повторном запуске не качаются заново.",
        ]
        return "\n".join(lines)

    def _build_run_bar(self) -> None:
        bar = ttk.Frame(self)
        bar.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        bar.columnconfigure(2, weight=1)
        self.run_btn = ttk.Button(bar, text="Импортировать данные", command=self._start_export)
        self.run_btn.grid(row=0, column=0)
        self.stop_btn = ttk.Button(bar, text="Остановить", command=self._stop_export, state="disabled")
        self.stop_btn.grid(row=0, column=1, padx=6)
        self.progress = ttk.Progressbar(bar, mode="determinate")
        self.progress.grid(row=0, column=2, sticky="ew", padx=10)
        self.status_var = tk.StringVar(value="Готов к работе.")
        ttk.Label(bar, textvariable=self.status_var, width=42, anchor="w").grid(row=0, column=3)

    def _market_key(self) -> str:
        label = self.market_label_var.get()
        for key, info in MARKETS.items():
            if info.label == label:
                return key
        return "shares"

    def _period_key(self) -> str:
        return PERIOD_LABELS.get(self.period_label_var.get(), "1min")

    def _selected_datasets(self) -> list[str]:
        return [key for key, var in self.dataset_vars.items() if var.get()]

    def _on_market_change(self, load_tickers: bool = False) -> None:
        market = self._market_key()
        self._rebuild_datasets(market)
        if market != self._current_market:
            self.selected_secids.clear()
        self._current_market = market
        self.selected_count_var.set(f"Выбрано: {len(self.selected_secids)}")
        self._update_plan()
        if load_tickers:
            self._load_tickers_async()

    def _rebuild_datasets(self, market: str) -> None:
        for child in self.datasets_frame.winfo_children():
            child.destroy()
        self.dataset_vars = {}
        available = datasets_for_market(market)
        remembered = set(self.settings.data_types)
        for row, dataset in enumerate(available):
            selected = dataset.key in remembered or (not remembered and dataset.default_selected)
            var = tk.BooleanVar(value=selected)
            var.trace_add("write", lambda *a: self._update_plan())
            self.dataset_vars[dataset.key] = var
            box = ttk.Checkbutton(self.datasets_frame, text=dataset.label, variable=var)
            box.grid(row=row, column=0, sticky="nw", pady=(0, 2))
            ttk.Label(
                self.datasets_frame, text=dataset.description, foreground="#666", wraplength=300,
                justify="left",
            ).grid(row=row, column=1, sticky="w", padx=(10, 0), pady=(0, 2))
        self.datasets_frame.columnconfigure(1, weight=1)
        if not any(var.get() for var in self.dataset_vars.values()) and self.dataset_vars:
            first = next(iter(self.dataset_vars.values()))
            first.set(True)

    def _on_full_history_toggle(self) -> None:
        state = "disabled" if self.full_history_var.get() else "normal"
        self.start_entry.configure(state=state)
        self.end_entry.configure(state=state)
        self.quick_combo.configure(state="disabled" if state == "disabled" else "readonly")
        self._update_plan()

    def _apply_quick_range(self) -> None:
        label = self.quick_var.get()
        today = moscow_today()
        if label == "С начала года":
            self.start_var.set(date(today.year, 1, 1).isoformat())
        else:
            days = QUICK_RANGES.get(label)
            if days is None:
                return
            self.start_var.set((today - timedelta(days=days)).isoformat())
        self.end_var.set(today.isoformat())

    def _parse_dates(self) -> tuple[date, date] | None:
        try:
            start = date.fromisoformat(self.start_var.get().strip())
            end = date.fromisoformat(self.end_var.get().strip())
        except ValueError:
            return None
        return start, end

    def _update_plan(self) -> None:
        if not self._ui_ready:
            return
        datasets = self._selected_datasets()
        if not datasets:
            self.plan_var.set("Выберите хотя бы один тип данных.")
            return
        if self.full_history_var.get():
            self.plan_var.set(
                "План: качается вся доступная история. Диапазон дат определяется отдельно "
                "по каждой паре «тикер × набор данных» перед выгрузкой, итоговое число "
                "запросов появится в журнале."
            )
            return
        dates = self._parse_dates()
        if not dates:
            self.plan_var.set("Укажите корректные даты и хотя бы один тип данных.")
            return
        start, end = dates
        if start > end:
            self.plan_var.set("Дата начала позже даты окончания.")
            return
        try:
            job = self._build_job(preview=True)
        except MoexDataError as exc:
            self.plan_var.set(str(exc))
            return
        parts = []
        total = 0
        for key in datasets:
            chunks = split_range(start, end, job.chunk_days_for(key), job.skip_weekends)
            total += len(chunks)
            span = "весь период" if job.chunk_days_for(key) <= 0 else f"по {job.chunk_days_for(key)} дн."
            parts.append(f"{DATASETS[key].label}: {len(chunks)} зап. ({span})")
        tickers = max(len(self.selected_secids), 1)
        self.plan_var.set(
            f"План: {total * tickers} запрос(ов) к API для {tickers} тикер(ов). " + "; ".join(parts)
        )

    def _load_tickers_async(self) -> None:
        market = self._market_key()
        api_key = self.api_key_var.get().strip() or self.settings.resolved_api_key()
        self.ticker_list.delete(0, "end")
        if not api_key:
            self.ticker_list.insert("end", "Введите API-ключ и нажмите «Проверить ключ»")
            self.status_var.set("Укажите API-ключ Algopack вверху окна.")
            return
        self.ticker_list.insert("end", "Загрузка списка инструментов…")
        self.status_var.set("Загрузка списка инструментов…")

        def work() -> None:
            try:
                df = client.list_tickers(market, api_key)
                items = [
                    (str(row.secid), f"{row.secid} — {row.shortname}")
                    for row in df.itertuples(index=False)
                ]
                self.queue.put(("tickers", items))
            except Exception as exc:  # noqa: BLE001
                self.queue.put(("tickers_error", classify(exc)))

        threading.Thread(target=work, daemon=True).start()

    def _apply_filter(self) -> None:
        query = self.search_var.get().strip().lower()
        self.ticker_list.delete(0, "end")
        self.visible_secids = []
        for secid, label in self.all_tickers:
            if query and query not in label.lower():
                continue
            self.ticker_list.insert("end", label)
            self.visible_secids.append(secid)
        for index, secid in enumerate(self.visible_secids):
            if secid in self.selected_secids:
                self.ticker_list.selection_set(index)

    def _on_listbox_select(self) -> None:
        chosen = {self.visible_secids[i] for i in self.ticker_list.curselection()}
        self.selected_secids -= set(self.visible_secids)
        self.selected_secids |= chosen
        self.selected_count_var.set(f"Выбрано: {len(self.selected_secids)}")
        self._update_plan()

    def _select_all_visible(self) -> None:
        self.ticker_list.selection_set(0, "end")
        self._on_listbox_select()

    def _clear_selection(self) -> None:
        self.ticker_list.selection_clear(0, "end")
        self._on_listbox_select()

    def _choose_folder(self) -> None:
        initial = self.out_dir_var.get() or str(DEFAULT_EXPORT_DIR)
        chosen = filedialog.askdirectory(initialdir=initial, title="Папка для выгрузки")
        if chosen:
            self.out_dir_var.set(chosen)

    def _open_folder(self) -> None:
        path = Path(self.out_dir_var.get().strip() or DEFAULT_EXPORT_DIR)
        try:
            path.mkdir(parents=True, exist_ok=True)
            if sys.platform.startswith("win"):
                os.startfile(path)  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Ошибка", f"Не удалось открыть папку:\n{classify(exc)}")

    def _clear_cache(self) -> None:
        import shutil

        from moexdata.core.exporter import CACHE_DIRNAME

        cache_dir = Path(self.out_dir_var.get().strip() or DEFAULT_EXPORT_DIR) / CACHE_DIRNAME
        if not cache_dir.exists():
            messagebox.showinfo("Кеш", "Кеш пуст — удалять нечего.")
            return
        if not messagebox.askyesno("Кеш", f"Удалить кеш частей?\n{cache_dir}"):
            return
        try:
            shutil.rmtree(cache_dir)
            self._log("info", f"Кеш удалён: {cache_dir}")
            messagebox.showinfo("Кеш", "Кеш удалён.")
        except OSError as exc:
            messagebox.showerror("Ошибка", f"Не удалось удалить кеш:\n{exc}")

    def _collect_settings(self) -> Settings:
        s = self.settings
        s.api_key = self.api_key_var.get().strip()
        s.market = self._market_key()
        s.tickers = sorted(self.selected_secids)
        s.data_types = self._selected_datasets() or s.data_types
        s.candle_period = self._period_key()
        s.start_date = self.start_var.get().strip()
        s.end_date = self.end_var.get().strip()
        s.full_history = self.full_history_var.get()
        s.out_dir = self.out_dir_var.get().strip() or str(DEFAULT_EXPORT_DIR)
        s.file_format = self.format_var.get()
        s.filename_template = self.template_var.get().strip() or s.filename_template
        s.subfolder_per_ticker = self.subfolder_var.get()
        s.chunk_mode = self.chunk_mode_var.get()
        s.chunk_days = _to_int(self.chunk_days_var.get(), 1)
        s.skip_weekends = self.skip_weekends_var.get()
        s.retries = _to_int(self.retries_var.get(), 3)
        s.retry_pause = _to_float(self.retry_pause_var.get(), 3.0)
        s.request_pause = _to_float(self.request_pause_var.get(), 0.0)
        s.use_cache = self.cache_var.get()
        s.keep_parts = self.keep_parts_var.get()
        s.dedupe = self.dedupe_var.get()
        s.sort_rows = self.sort_var.get()
        s.add_ticker_column = self.add_ticker_var.get()
        s.parse_dates = self.parse_dates_var.get()
        s.use_calendar = self.calendar_var.get()
        s.validate_data = self.validate_var.get()
        s.update_existing = self.update_var.get()
        s.adjust_prices = self.adjust_var.get()
        s.continuous = self.continuous_var.get()
        s.roll_days = _to_int(self.roll_days_var.get(), 5)
        s.roll_adjust = next(
            (k for k, v in ROLL_ADJUST_LABELS.items() if v == self.roll_adjust_var.get()), "diff"
        )
        s.quarterly_only = self.quarterly_var.get()
        s.combine_tickers = self.combine_var.get()
        s.write_manifest = self.manifest_var.get()
        s.csv_sep = self.csv_sep_var.get() or ","
        s.csv_decimal = self.csv_decimal_var.get() or "."
        s.csv_encoding = self.csv_encoding_var.get() or "utf-8-sig"
        s.csv_gzip = self.csv_gzip_var.get()
        s.parquet_compression = self.parquet_compression_var.get()
        s.normalize()
        return s

    def _save_settings_clicked(self) -> None:
        try:
            path = self._collect_settings().save()
        except OSError as exc:
            messagebox.showerror("Ошибка", f"Не удалось сохранить настройки:\n{exc}")
            return
        client.reset()
        self.key_status_var.set(f"Настройки сохранены: {path}")
        self._log("info", f"Настройки сохранены: {path}")

    def _check_key_clicked(self) -> None:
        api_key = self.api_key_var.get().strip()
        if not api_key:
            self.key_status_var.set("Ключ не задан.")
            messagebox.showerror("Ошибка", "Введите API-ключ Algopack.")
            return
        self.key_status_var.set("Проверка ключа…")

        def work() -> None:
            ok, message = client.check_api_key(api_key)
            self.queue.put(("key_checked", (ok, message)))

        threading.Thread(target=work, daemon=True).start()

    def _build_job(self, preview: bool = False) -> ExportJob:
        settings = self._collect_settings()
        dates = self._parse_dates()
        if dates is None:
            if not settings.full_history:
                raise MoexDataError("Даты должны быть в формате ГГГГ-ММ-ДД.")
            today = moscow_today()
            dates = (today - timedelta(days=30), today)
        start, end = dates
        tickers = sorted(self.selected_secids) or (["PREVIEW"] if preview else [])
        return ExportJob(
            api_key=settings.resolved_api_key(),
            market=settings.market,
            tickers=tickers,
            datasets=self._selected_datasets(),
            start=start,
            end=end,
            out_dir=Path(settings.out_dir),
            candle_period=settings.candle_period,
            file_format=settings.file_format,
            full_history=settings.full_history,
            chunk_mode=settings.chunk_mode,
            chunk_days=settings.chunk_days,
            skip_weekends=settings.skip_weekends,
            retries=settings.retries,
            retry_pause=settings.retry_pause,
            request_pause=settings.request_pause,
            use_cache=settings.use_cache,
            keep_parts=settings.keep_parts,
            dedupe=settings.dedupe,
            sort_rows=settings.sort_rows,
            add_ticker_column=settings.add_ticker_column,
            parse_dates=settings.parse_dates,
            use_calendar=settings.use_calendar,
            validate_data=settings.validate_data,
            adjust_prices=settings.adjust_prices,
            update_existing=settings.update_existing,
            continuous=settings.continuous and settings.market == "futures",
            roll_days=settings.roll_days,
            roll_adjust=settings.roll_adjust,
            quarterly_only=settings.quarterly_only,
            combine_tickers=settings.combine_tickers,
            subfolder_per_ticker=settings.subfolder_per_ticker,
            write_manifest=settings.write_manifest,
            filename_template=settings.filename_template,
            csv_sep=settings.csv_sep,
            csv_decimal=settings.csv_decimal,
            csv_encoding=settings.csv_encoding,
            csv_gzip=settings.csv_gzip,
            parquet_compression=settings.parquet_compression,
            only_pairs=self.retry_pairs,
        )

    def _start_export(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            job = self._build_job()
            job.validate()
        except MoexDataError as exc:
            messagebox.showerror("Проверьте параметры", str(exc))
            self.retry_pairs = None
            return
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Ошибка", str(classify(exc)))
            self.retry_pairs = None
            return

        if job.full_history and not self._confirm_full_history(job):
            self.retry_pairs = None
            return

        try:
            self.settings.save()
        except OSError:
            pass

        self.stop_flag = False
        self.last_summary = None
        self.run_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.progress.configure(value=0, maximum=100)
        for item in self.errors_tree.get_children():
            self.errors_tree.delete(item)
        self.notebook.select(self.tab_log)
        self._log(
            "info",
            f"Запуск: {len(job.tickers)} тикер(ов) × {len(job.datasets)} набор(ов), "
            f"{job.start} — {job.end}, формат {job.file_format} → {job.out_dir}",
        )

        def work() -> None:
            try:
                summary = run_export(
                    job,
                    progress_cb=lambda done, total, msg: self.queue.put(("progress", (done, total, msg))),
                    log_cb=lambda level, msg: self.queue.put(("log", (level, msg))),
                    should_stop=lambda: self.stop_flag,
                )
                self.queue.put(("done", summary))
            except Exception as exc:  # noqa: BLE001
                self.queue.put(("fatal", classify(exc)))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _confirm_full_history(self, job: ExportJob) -> bool:
        minute_candles = "candles" in job.datasets and job.chunk_days_for("candles") <= 5
        if not minute_candles:
            return True
        message = "\n".join(
            (
                f"Выбраны свечи с мелким интервалом и вся доступная история "
                f"по {len(job.tickers)} инструмент(ам).",
                "",
                "Это тысячи запросов к API и, скорее всего, несколько часов работы.",
                "Прерванную выгрузку можно продолжить позже — скачанное лежит в кеше.",
                "",
                "Начать?",
            )
        )
        return messagebox.askyesno("Вся доступная история", message)

    def _stop_export(self) -> None:
        self.stop_flag = True
        self.stop_btn.configure(state="disabled")
        self.status_var.set("Останавливаю…")
        self._log("warn", "Запрошена остановка выгрузки.")

    def _retry_failed(self) -> None:
        if not self.last_summary:
            messagebox.showinfo("Повтор", "Сначала выполните выгрузку.")
            return
        failed = self.last_summary.failed_pairs()
        if not failed:
            messagebox.showinfo("Повтор", "Неудачных выгрузок нет.")
            return
        self.retry_pairs = failed
        self._log("info", f"Повтор для {len(failed)} пар(ы) «тикер × набор».")
        self._start_export()

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "tickers":
                    self.all_tickers = payload
                    self._apply_filter()
                    self._on_listbox_select()
                    self.status_var.set(f"Инструментов загружено: {len(payload)}")
                    self._log("info", f"Список инструментов обновлён: {len(payload)}")
                elif kind == "tickers_error":
                    self.ticker_list.delete(0, "end")
                    self.status_var.set("Не удалось загрузить список инструментов.")
                    self._log("error", f"Список инструментов: {payload}")
                    messagebox.showerror("Ошибка", f"Не удалось загрузить инструменты:\n{payload}")
                elif kind == "key_checked":
                    ok, message = payload
                    self.key_status_var.set(message)
                    self.key_status.configure(
                        foreground=LEVEL_COLORS["ok"] if ok else LEVEL_COLORS["error"]
                    )
                    self._log("ok" if ok else "error", f"Проверка ключа: {message}")
                    if ok:
                        self._load_tickers_async()
                elif kind == "progress":
                    done, total, message = payload
                    self.progress.configure(maximum=max(total, 1), value=done)
                    percent = int(done * 100 / max(total, 1))
                    self.status_var.set(f"{percent}% ({done}/{total}) {message}"[:80])
                elif kind == "log":
                    level, message = payload
                    self._log(level, message)
                elif kind == "done":
                    self._on_export_done(payload)
                elif kind == "fatal":
                    self.retry_pairs = None
                    self._log("error", f"Выгрузка прервана: {payload}")
                    self.status_var.set("Выгрузка прервана ошибкой.")
                    self.run_btn.configure(state="normal")
                    self.stop_btn.configure(state="disabled")
                    messagebox.showerror("Ошибка выгрузки", str(payload))
        except queue.Empty:
            pass
        self.master.after(100, self._poll_queue)

    def _on_export_done(self, summary: ExportSummary) -> None:
        self.retry_pairs = None
        self.last_summary = summary
        self.run_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self._fill_errors_table(summary)

        head = "Выгрузка остановлена" if summary.stopped else "Выгрузка завершена"
        text = (
            f"Успешно: {summary.ok}\n"
            f"Нет данных: {summary.empty}\n"
            f"Ошибок: {summary.failed}\n"
            f"Строк всего: {summary.total_rows}\n"
            f"Папка: {self.out_dir_var.get()}"
        )
        self.status_var.set(
            f"{head}: успешно {summary.ok}, пусто {summary.empty}, ошибок {summary.failed}"
        )
        if summary.warnings:
            text += f"\n\nПредупреждений: {len(summary.warnings)} (см. вкладку «Журнал»)"
        if summary.failed:
            self.notebook.select(self.tab_errors)
            messagebox.showwarning(head, text)
        else:
            messagebox.showinfo(head, text)

    def _fill_errors_table(self, summary: ExportSummary) -> None:
        for item in self.errors_tree.get_children():
            self.errors_tree.delete(item)
        for result in summary.results:
            dataset = DATASETS.get(result.dataset)
            self.errors_tree.insert(
                "",
                "end",
                values=(
                    result.ticker,
                    dataset.label if dataset else result.dataset,
                    STATUS_LABELS.get(result.status, result.status),
                    f"{result.start} — {result.end}" if result.start else "",
                    result.rows,
                    result.code,
                    result.message or (result.path or ""),
                ),
                tags=(result.status,),
            )
        self.errors_summary_var.set(
            f"Успешно: {summary.ok} · Нет данных: {summary.empty} · Ошибок: {summary.failed}"
        )

    def _log(self, level: str, message: str) -> None:
        if level == "debug" and not self.verbose_var.get():
            return
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n", level)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _save_log(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Сохранить журнал",
            defaultextension=".txt",
            filetypes=[("Текстовый файл", "*.txt")],
            initialfile="moexdata_log.txt",
        )
        if not path:
            return
        try:
            Path(path).write_text(self.log_text.get("1.0", "end"), encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("Ошибка", f"Не удалось сохранить журнал:\n{exc}")

    def _save_error_report(self) -> None:
        if not self.last_summary:
            messagebox.showinfo("Отчёт", "Сначала выполните выгрузку.")
            return
        path = filedialog.asksaveasfilename(
            title="Сохранить отчёт",
            defaultextension=".json",
            filetypes=[("JSON", "*.json")],
            initialfile="moexdata_report.json",
        )
        if not path:
            return
        import json

        try:
            Path(path).write_text(
                json.dumps(self.last_summary.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            messagebox.showerror("Ошибка", f"Не удалось сохранить отчёт:\n{exc}")

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("Выход", "Выгрузка ещё идёт. Прервать и выйти?"):
                return
            self.stop_flag = True
        try:
            self._collect_settings().save()
        except OSError:
            pass
        self.master.destroy()


def _to_int(value: str, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _to_float(value: str, default: float) -> float:
    try:
        return float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return default


def main() -> None:
    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.2)
    except tk.TclError:
        pass
    MoexDataApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
