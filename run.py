from __future__ import annotations

import sys


def main() -> int:
    try:
        from moexdata.gui.app import main as gui_main
    except ImportError as exc:
        print(f"Не удалось запустить приложение: {exc}", file=sys.stderr)
        print("Установите зависимости: pip install -r requirements.txt", file=sys.stderr)
        return 1
    gui_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
