from __future__ import annotations

from dataclasses import dataclass


class MoexDataError(Exception):
    pass


class ApiKeyError(MoexDataError):
    pass


class ConfigError(MoexDataError):
    pass


@dataclass
class ExportError(MoexDataError):
    code: str
    message: str
    retryable: bool = False
    hint: str = ""
    original: str = ""

    def __str__(self) -> str:
        return self.message if not self.hint else f"{self.message} {self.hint}"


CODE_NO_KEY = "no_api_key"
CODE_AUTH = "auth"
CODE_FORBIDDEN = "forbidden"
CODE_RATE_LIMIT = "rate_limit"
CODE_NOT_FOUND = "not_found"
CODE_SERVER = "server"
CODE_NETWORK = "network"
CODE_TIMEOUT = "timeout"
CODE_BAD_RESPONSE = "bad_response"
CODE_UNKNOWN_TICKER = "unknown_ticker"
CODE_UNSUPPORTED = "unsupported"
CODE_BAD_PARAMS = "bad_params"
CODE_DISK = "disk"
CODE_DEPENDENCY = "dependency"
CODE_UNKNOWN = "unknown"

_HTTP_MESSAGES = {
    400: (CODE_BAD_PARAMS, "Некорректный запрос к API (400).", "Проверьте даты и период.", False),
    401: (CODE_AUTH, "API-ключ не принят (401).", "Проверьте ключ в настройках.", False),
    403: (
        CODE_FORBIDDEN,
        "Доступ запрещён (403).",
        "Ключ недействителен либо на этот набор данных нет подписки Algopack.",
        False,
    ),
    404: (CODE_NOT_FOUND, "Данные не найдены (404).", "Возможно, тикер не торгуется на выбранном рынке.", False),
    422: (CODE_BAD_PARAMS, "API отклонило параметры запроса (422).", "Проверьте период свечей и диапазон дат.", False),
    429: (
        CODE_RATE_LIMIT,
        "Превышен лимит запросов (429).",
        "Увеличьте паузу между запросами в разделе «Дополнительно».",
        True,
    ),
    500: (CODE_SERVER, "Внутренняя ошибка сервера MOEX (500).", "Повторите попытку позже.", True),
    502: (CODE_SERVER, "Шлюз MOEX недоступен (502).", "Повторите попытку позже.", True),
    503: (CODE_SERVER, "Сервис MOEX временно недоступен (503).", "Повторите попытку позже.", True),
    504: (CODE_SERVER, "Таймаут шлюза MOEX (504).", "Повторите попытку позже.", True),
}


def classify(exc: BaseException) -> ExportError:
    if isinstance(exc, ExportError):
        return exc
    if isinstance(exc, ApiKeyError):
        return ExportError(CODE_NO_KEY, str(exc), False, "Заполните поле «API-ключ» вверху окна.", repr(exc))

    module = type(exc).__module__.split(".")[0]
    name = type(exc).__name__
    text = str(exc)

    if module == "httpx":
        if name == "HTTPStatusError":
            status = getattr(getattr(exc, "response", None), "status_code", None)
            known = _HTTP_MESSAGES.get(status)
            if known:
                code, message, hint, retryable = known
                return ExportError(code, message, retryable, hint, text)
            if status and 500 <= status < 600:
                return ExportError(CODE_SERVER, f"Ошибка сервера MOEX ({status}).", True, "Повторите позже.", text)
            return ExportError(CODE_UNKNOWN, f"HTTP-ошибка {status}.", False, "", text)
        if "Timeout" in name:
            return ExportError(
                CODE_TIMEOUT, "Истекло время ожидания ответа MOEX.", True, "Проверьте интернет-соединение.", text
            )
        return ExportError(
            CODE_NETWORK,
            "Ошибка соединения с MOEX.",
            True,
            "Проверьте интернет-соединение, прокси и доступность iss.moex.com.",
            text,
        )

    if isinstance(exc, LookupError) and "Unrecognized" in text:
        return ExportError(
            CODE_UNKNOWN_TICKER,
            "Инструмент не найден на выбранном рынке.",
            False,
            "Обновите список тикеров и выберите инструмент заново.",
            text,
        )
    if isinstance(exc, NotImplementedError):
        return ExportError(
            CODE_UNSUPPORTED,
            "Этот набор данных недоступен для выбранного рынка.",
            False,
            "Выберите другой тип данных.",
            text,
        )
    if isinstance(exc, ImportError):
        return ExportError(
            CODE_DEPENDENCY,
            f"Не установлена зависимость: {text}",
            False,
            "Установите пакеты командой: pip install -r requirements.txt",
            text,
        )
    if isinstance(exc, (PermissionError, OSError)) and not isinstance(exc, TimeoutError):
        return ExportError(
            CODE_DISK,
            f"Не удалось записать файл: {text}",
            False,
            "Проверьте права на папку выгрузки и свободное место на диске.",
            text,
        )
    if isinstance(exc, ValueError) and "wrong data" in text.lower():
        return ExportError(
            CODE_BAD_RESPONSE, "MOEX вернул некорректный ответ.", True, "Повторите попытку.", text
        )
    if isinstance(exc, ValueError):
        return ExportError(CODE_BAD_PARAMS, f"Некорректные параметры: {text}", False, "", text)

    return ExportError(CODE_UNKNOWN, f"Непредвиденная ошибка: {name}: {text}", False, "", repr(exc))
