from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

from moexdata.api import client, iss
from moexdata.settings import DEFAULT_EXPORT_DIR, Settings
from moexdata.timeutil import moscow_today


def _describe(raw: dict) -> dict:
    result = {}
    for name, block in (raw or {}).items():
        if isinstance(block, dict) and "columns" in block:
            rows = block.get("data") or []
            result[name] = {"columns": block["columns"], "rows": len(rows), "head": rows[:5], "tail": rows[-3:]}
        else:
            result[name] = repr(block)[:300]
    return result


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    share = (args[0] if args else "SBER").upper()
    asset = args[1] if len(args) > 1 else "Si"
    key = Settings.load().resolved_api_key()
    if key:
        client.configure(key)
    today = moscow_today()
    requests = [
        ("calendars", {"from": (today - timedelta(days=40)).isoformat(), "till": today.isoformat(),
                       "show_all_days": 1}),
        (f"securities/{share}/dividends", {}),
        ("cci/corp-actions/dividends", {"secid": share}),
        ("cci/corp-actions", {"secid": share}),
        (f"statistics/engines/stock/splits/{share}", {}),
        ("statistics/engines/stock/splits", {}),
        ("statistics/engines/futures/markets/forts/series", {"asset_code": asset, "show_expired": 1}),
    ]
    report = {"date": today.isoformat(), "api_key_set": bool(key), "requests": []}
    for path, params in requests:
        for host in iss.HOSTS:
            entry = {"path": path, "params": params, "host": host}
            try:
                raw = iss.get_json(path, params, host)
                entry["blocks"] = _describe(raw)
                entry["rows"] = iss.rows_in(raw)
            except Exception as exc:  # noqa: BLE001
                status = getattr(getattr(exc, "response", None), "status_code", None)
                entry["error"] = f"{type(exc).__name__}: {exc}"[:500]
                entry["status"] = status
            report["requests"].append(entry)
            state = entry.get("error") or f"строк {entry.get('rows')}"
            print(f"{host:9} {path:55} {state}")

    out = Path(DEFAULT_EXPORT_DIR) / "iss_diag.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"\nПодробности сохранены: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
