import base64
import json
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


class KalshiOrderClient:
    """
    Kalshi order client for /portfolio/orders with robust auth-path handling.

    Notes:
    - Uses KALSHI-ACCESS-* headers and optional backward-compatible KALSHI-API-*.
    - Supports signing path with or without /trade-api/v2 prefix (auto mode).
    - Caller decides when to enable live posting (safe-by-default architecture).
    """

    def __init__(
        self,
        api_key_id: str,
        private_key_path: str,
        base_url: str = "https://api.elections.kalshi.com/trade-api/v2",
        timeout_sec: float = 10.0,
        sign_path_mode: str = "auto",
        auth_retry_on_401: bool = True,
        include_legacy_headers: bool = True,
    ) -> None:
        self.api_key_id = str(api_key_id or "").strip()
        self.base_url = str(base_url or "").rstrip("/")
        self.timeout_sec = float(timeout_sec)
        self.sign_path_mode = str(sign_path_mode or "auto").strip().lower()
        self.auth_retry_on_401 = bool(auth_retry_on_401)
        self.include_legacy_headers = bool(include_legacy_headers)
        self.session = requests.Session()
        if self.sign_path_mode not in {"auto", "with_base", "without_base"}:
            raise ValueError("sign_path_mode must be auto|with_base|without_base")
        if not self.api_key_id:
            raise ValueError("kalshi api_key_id is required")

        parsed = urlparse(self.base_url)
        base_prefix = str(parsed.path or "").strip()
        self.base_prefix = base_prefix.rstrip("/") if base_prefix not in {"", "/"} else ""

        key_path = Path(private_key_path).resolve()
        if not key_path.exists():
            raise FileNotFoundError(f"kalshi private key file not found: {key_path}")
        with key_path.open("rb") as fh:
            self._private_key = serialization.load_pem_private_key(fh.read(), password=None)

    @staticmethod
    def _canonical_path(path_or_url: str) -> str:
        raw = str(path_or_url or "").strip()
        if not raw:
            raise ValueError("path is required")
        if raw.startswith("http://") or raw.startswith("https://"):
            parsed = urlparse(raw)
            path = parsed.path or "/"
        else:
            path = raw if raw.startswith("/") else f"/{raw}"
        out = path.split("?", 1)[0]
        return out if out.startswith("/") else f"/{out}"

    def _with_base_path(self, canonical_path: str) -> str:
        path = self._canonical_path(canonical_path)
        if not self.base_prefix:
            return path
        if path == self.base_prefix or path.startswith(self.base_prefix + "/"):
            return path
        return f"{self.base_prefix}{path}"

    def _without_base_path(self, canonical_path: str) -> str:
        path = self._canonical_path(canonical_path)
        if not self.base_prefix:
            return path
        if path == self.base_prefix:
            return "/"
        if path.startswith(self.base_prefix + "/"):
            trimmed = path[len(self.base_prefix) :]
            return trimmed if trimmed.startswith("/") else f"/{trimmed}"
        return path

    def _signing_candidates(self, path_or_url: str) -> list[str]:
        canonical = self._canonical_path(path_or_url)
        with_base = self._with_base_path(canonical)
        without_base = self._without_base_path(canonical)
        if self.sign_path_mode == "with_base":
            return [with_base]
        if self.sign_path_mode == "without_base":
            return [without_base]
        if with_base == without_base:
            return [with_base]
        return [with_base, without_base]

    def _headers(self, method: str, signing_path: str, content_json: bool = False) -> dict[str, str]:
        meth = str(method or "").upper().strip()
        if not meth:
            raise ValueError("http method is required")
        sign_path = self._canonical_path(signing_path)
        ts = str(int(time.time() * 1000))
        message = ts + meth + sign_path
        sig = self._private_key.sign(
            message.encode("utf-8"),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
            hashes.SHA256(),
        )
        sig_b64 = base64.b64encode(sig).decode("utf-8")
        headers = {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sig_b64,
        }
        if self.include_legacy_headers:
            headers["KALSHI-API-KEY"] = self.api_key_id
            headers["KALSHI-API-TIMESTAMP"] = ts
            headers["KALSHI-API-SIGNATURE"] = sig_b64
        if content_json:
            headers["Content-Type"] = "application/json"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> requests.Response:
        path_no_query = self._canonical_path(path)
        url_path = path_no_query
        if self.base_prefix and (url_path == self.base_prefix or url_path.startswith(self.base_prefix + "/")):
            url_path = self._without_base_path(url_path)
        url = self.base_url + url_path
        candidates = self._signing_candidates(path_no_query)
        meth = str(method).upper()
        last_resp: Optional[requests.Response] = None
        for idx, sign_path in enumerate(candidates):
            headers = self._headers(meth, sign_path, content_json=payload is not None)
            resp = self.session.request(
                method=meth,
                url=url,
                params=params,
                data=None if payload is None else json.dumps(payload),
                headers=headers,
                timeout=self.timeout_sec,
            )
            last_resp = resp
            if resp.status_code != 401:
                return resp
            if (not self.auth_retry_on_401) or idx >= (len(candidates) - 1):
                return resp
        if last_resp is None:
            raise RuntimeError("kalshi_request_failed_without_response")
        return last_resp

    @staticmethod
    def _normalize_side(value: str) -> str:
        out = str(value or "").strip().lower()
        if out not in {"yes", "no"}:
            raise ValueError("side must be 'yes' or 'no'")
        return out

    @staticmethod
    def _normalize_action(value: str) -> str:
        out = str(value or "").strip().lower()
        if out not in {"buy", "sell"}:
            raise ValueError("action must be 'buy' or 'sell'")
        return out

    @staticmethod
    def _validate_cent_price(name: str, value: Optional[int]) -> Optional[int]:
        if value is None:
            return None
        iv = int(value)
        if iv < 1 or iv > 99:
            raise ValueError(f"{name} must be between 1 and 99 cents")
        return iv

    def create_order(
        self,
        *,
        ticker: str,
        side: str,
        action: str,
        order_type: str = "limit",
        client_order_id: Optional[str] = None,
        count: Optional[int] = None,
        count_fp: Optional[str] = None,
        yes_price: Optional[int] = None,
        no_price: Optional[int] = None,
        yes_price_dollars: Optional[str] = None,
        no_price_dollars: Optional[str] = None,
        expiration_ts: Optional[int] = None,
        time_in_force: Optional[str] = None,
        post_only: Optional[bool] = None,
        buy_max_cost: Optional[int] = None,
        sell_position_floor: Optional[int] = None,
        subaccount: Optional[int] = None,
        reduce_only: Optional[bool] = None,
        cancel_order_on_pause: Optional[bool] = None,
        self_trade_prevention_type: Optional[str] = None,
        order_group_id: Optional[str] = None,
    ) -> dict[str, Any]:
        tick = str(ticker or "").strip()
        if not tick:
            raise ValueError("ticker is required")
        otype = str(order_type or "limit").strip().lower()
        if otype not in {"limit"}:
            raise ValueError("order_type must be 'limit'")
        body: dict[str, Any] = {
            "ticker": tick,
            "type": otype,
            "side": self._normalize_side(side),
            "action": self._normalize_action(action),
        }
        if client_order_id:
            body["client_order_id"] = str(client_order_id).strip()
        if order_group_id:
            body["order_group_id"] = str(order_group_id).strip()

        if count is None and (count_fp is None or str(count_fp).strip() == ""):
            raise ValueError("count or count_fp is required")
        if count is not None:
            body["count"] = int(count)
        if count_fp is not None and str(count_fp).strip():
            body["count_fp"] = str(count_fp).strip()

        y_cents = self._validate_cent_price("yes_price", yes_price)
        n_cents = self._validate_cent_price("no_price", no_price)
        if y_cents is not None:
            body["yes_price"] = y_cents
        if n_cents is not None:
            body["no_price"] = n_cents
        if yes_price_dollars is not None and str(yes_price_dollars).strip():
            body["yes_price_dollars"] = str(yes_price_dollars).strip()
        if no_price_dollars is not None and str(no_price_dollars).strip():
            body["no_price_dollars"] = str(no_price_dollars).strip()

        any_price = any(
            x is not None and str(x).strip() != ""
            for x in (y_cents, n_cents, yes_price_dollars, no_price_dollars)
        )
        if not any_price:
            raise ValueError("at least one price field is required")

        if expiration_ts is not None:
            body["expiration_ts"] = int(expiration_ts)
        if time_in_force:
            tif = str(time_in_force).strip().lower()
            if tif not in {"fill_or_kill", "good_till_canceled", "immediate_or_cancel"}:
                raise ValueError("invalid time_in_force")
            body["time_in_force"] = tif
        if post_only is not None:
            body["post_only"] = bool(post_only)
        if buy_max_cost is not None:
            body["buy_max_cost"] = int(buy_max_cost)
        if sell_position_floor is not None:
            body["sell_position_floor"] = int(sell_position_floor)
        if subaccount is not None:
            body["subaccount"] = int(subaccount)
        if reduce_only is not None:
            body["reduce_only"] = bool(reduce_only)
        if cancel_order_on_pause is not None:
            body["cancel_order_on_pause"] = bool(cancel_order_on_pause)
        if self_trade_prevention_type:
            stp = str(self_trade_prevention_type).strip().lower()
            if stp not in {"taker_at_cross", "maker", "taker"}:
                raise ValueError("invalid self_trade_prevention_type")
            body["self_trade_prevention_type"] = stp

        resp = self._request("POST", "/portfolio/orders", payload=body)
        if resp.status_code >= 400:
            raise RuntimeError(f"kalshi_create_order_failed status={resp.status_code} body={resp.text}")
        return resp.json()

    def get_order(self, order_id: str) -> dict[str, Any]:
        oid = str(order_id or "").strip()
        if not oid:
            raise ValueError("order_id is required")
        resp = self._request("GET", f"/portfolio/orders/{oid}")
        if resp.status_code >= 400:
            raise RuntimeError(f"kalshi_get_order_failed status={resp.status_code} body={resp.text}")
        return resp.json()

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        oid = str(order_id or "").strip()
        if not oid:
            raise ValueError("order_id is required")
        resp = self._request("DELETE", f"/portfolio/orders/{oid}")
        if resp.status_code >= 400:
            raise RuntimeError(f"kalshi_cancel_order_failed status={resp.status_code} body={resp.text}")
        return resp.json()

    def list_orders(
        self,
        *,
        status: Optional[str] = None,
        ticker: Optional[str] = None,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if status:
            params["status"] = str(status).strip().lower()
        if ticker:
            params["ticker"] = str(ticker).strip()
        if limit is not None:
            params["limit"] = int(limit)
        if cursor:
            params["cursor"] = str(cursor).strip()
        resp = self._request("GET", "/portfolio/orders", params=params)
        if resp.status_code >= 400:
            raise RuntimeError(f"kalshi_list_orders_failed status={resp.status_code} body={resp.text}")
        return resp.json()
