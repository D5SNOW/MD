from __future__ import annotations

from typing import Any

ALGOPACK = "algopack"
PUBLIC = "public"
# Справочники через шлюз apim иногда приходят пустыми, на iss.moex.com они бесплатны.
HOSTS = (ALGOPACK, PUBLIC)
PUBLIC_URL = "https://iss.moex.com/iss"
TIMEOUT = 30.0


class NoData(Exception):
    pass


def get_json(path: str, params: dict[str, Any], host: str) -> dict[str, Any]:
    params = {"iss.meta": "off", **params}
    if host == ALGOPACK:
        from moexalgo.session import Session

        with Session(timeout=TIMEOUT) as client:
            return client.get_objects(path, lambda data: data, **params)

    import httpx
    from moexalgo._tls import create_moex_ssl_context

    url = f"{PUBLIC_URL}/{path.strip('/')}.json"
    with httpx.Client(verify=create_moex_ssl_context(trust_env=True), timeout=TIMEOUT,
                      follow_redirects=True, headers={"User-Agent": "moexdata"}) as client:
        response = client.get(url, params=params)
        response.raise_for_status()
        return response.json()


def rows_in(raw: dict[str, Any], block: str | None = None) -> int:
    blocks = [raw.get(block)] if block else list((raw or {}).values())
    return sum(len(b.get("data") or []) for b in blocks if isinstance(b, dict))


def fetch(path: str, params: dict[str, Any] | None = None, block: str | None = None) -> tuple[dict, str]:
    errors: list[Exception] = []
    for host in HOSTS:
        try:
            raw = get_json(path, dict(params or {}), host)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
            continue
        if rows_in(raw, block):
            return raw, host
    if errors and len(errors) == len(HOSTS):
        http = [e for e in errors if getattr(getattr(e, "response", None), "status_code", None)]
        raise (http or errors)[-1]
    raise NoData(path)


def table(raw: dict[str, Any], block: str):
    import pandas as pd

    data: dict[str, Any] = raw.get(block) or {}
    columns = [str(c).lower() for c in data.get("columns") or []]
    return pd.DataFrame(data.get("data") or [], columns=columns)
