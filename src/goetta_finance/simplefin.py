from __future__ import annotations

import base64
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from goetta_finance.errors import (
    BridgeAuthError,
    BridgeRateLimitError,
    BridgeUnavailableError,
    SetupTokenError,
    SimpleFinError,
)
from goetta_finance.models import Account, Transaction

logger = logging.getLogger(__name__)


def _ts_to_utc(value: Any) -> datetime:
    return datetime.fromtimestamp(int(value), tz=UTC)


def _maybe_ts_to_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    return _ts_to_utc(value)


def _to_decimal(value: Any, field: str, txn_id: str | None = None) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        ctx = f" (transaction {txn_id})" if txn_id else ""
        raise SimpleFinError(f"Invalid decimal for {field}{ctx}: {value!r}") from exc


def _maybe_decimal(value: Any, field: str) -> Decimal | None:
    if value is None:
        return None
    return _to_decimal(value, field)


def _split_access_url(access_url: str) -> tuple[str, tuple[str, str] | None]:
    """Return (base_url_without_auth, basic_auth_tuple_or_None)."""
    parts = urlsplit(access_url)
    if not parts.scheme or not parts.netloc:
        raise SimpleFinError("Access URL is not a valid URL")
    auth: tuple[str, str] | None = None
    netloc = parts.netloc
    if "@" in netloc:
        creds, host = netloc.rsplit("@", 1)
        if ":" in creds:
            user, _, pwd = creds.partition(":")
            auth = (user, pwd)
        netloc = host
    base = urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    return base.rstrip("/"), auth


def parse_accounts(raw: dict[str, Any]) -> list[Account]:
    out: list[Account] = []
    for a in raw.get("accounts", []):
        org = a.get("org") or {}
        out.append(
            Account(
                id=a["id"],
                org_id=org.get("domain") or org.get("sfin-url"),
                org_name=org.get("name"),
                name=a["name"],
                currency=a.get("currency") or "USD",
                balance=_to_decimal(a["balance"], "balance"),
                available_balance=_maybe_decimal(a.get("available-balance"), "available-balance"),
                balance_date=_ts_to_utc(a["balance-date"]),
                type=None,
                extra=dict(a.get("extra") or {}),
            )
        )
    return out


def parse_transactions(raw: dict[str, Any]) -> list[Transaction]:
    out: list[Transaction] = []
    for a in raw.get("accounts", []):
        account_id = a["id"]
        for tx in a.get("transactions") or []:
            if tx.get("pending"):
                continue
            out.append(
                Transaction(
                    id=tx["id"],
                    account_id=account_id,
                    posted=_ts_to_utc(tx["posted"]),
                    transacted_at=_maybe_ts_to_utc(tx.get("transacted_at")),
                    amount=_to_decimal(tx["amount"], "amount", tx.get("id")),
                    description=tx.get("description", ""),
                    payee=tx.get("payee"),
                    memo=tx.get("memo") or None,
                    pending=False,
                    extra=dict(tx.get("extra") or {}),
                )
            )
    return out


class SimpleFinClient:
    """Thin wrapper around the SimpleFIN Bridge HTTP API.

    The access URL contains basic-auth credentials; we never log it.
    """

    def __init__(self, access_url: str, *, timeout: float = 30.0) -> None:
        base, auth = _split_access_url(access_url)
        self._base = base
        self._auth = auth
        self._timeout = timeout

    @classmethod
    def claim(cls, setup_token: str) -> str:
        """Trade a one-shot setup token for an access URL.

        The setup token is base64-encoded bytes of a claim URL. POST to it
        with no body; the response body is the access URL.
        """
        try:
            claim_url = base64.b64decode(setup_token.strip()).decode("ascii")
        except (ValueError, UnicodeDecodeError) as exc:
            raise SetupTokenError("Setup token is not valid base64") from exc
        if not claim_url.startswith(("http://", "https://")):
            raise SetupTokenError("Setup token did not decode to an HTTP URL")
        try:
            response = httpx.post(claim_url, timeout=30.0)
        except httpx.HTTPError as exc:
            raise SetupTokenError(f"Failed to reach SimpleFIN claim URL: {exc}") from exc
        if response.status_code == 403:
            raise SetupTokenError("Setup token has already been claimed or is invalid")
        if response.status_code >= 400:
            raise SetupTokenError(f"SimpleFIN claim returned HTTP {response.status_code}")
        access_url = response.text.strip()
        if not access_url.startswith(("http://", "https://")):
            raise SetupTokenError("SimpleFIN claim response did not contain an access URL")
        return access_url

    def fetch(self, start: datetime, end: datetime) -> dict[str, Any]:
        params = {
            "start-date": int(start.timestamp()),
            "end-date": int(end.timestamp()),
            "pending": 0,
        }
        try:
            response = httpx.get(
                f"{self._base}/accounts",
                params=params,
                auth=self._auth,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise SimpleFinError(f"SimpleFIN request failed: {exc}") from exc
        if response.status_code in (401, 403):
            raise BridgeAuthError(
                f"SimpleFIN Bridge auth failed (HTTP {response.status_code}). "
                f"The access URL may be revoked; run `goetta-finance init` to "
                f"reclaim a new setup token."
            )
        if response.status_code == 429:
            retry_after = response.headers.get("retry-after")
            hint = f" (retry-after: {retry_after}s)" if retry_after else ""
            raise BridgeRateLimitError(
                f"SimpleFIN Bridge rate-limited the request{hint}. Back off "
                f"and try again later."
            )
        if 500 <= response.status_code < 600:
            raise BridgeUnavailableError(
                f"SimpleFIN Bridge unavailable (HTTP {response.status_code}). "
                f"This is usually transient; try again in a few minutes."
            )
        if response.status_code >= 400:
            raise SimpleFinError(f"SimpleFIN /accounts returned HTTP {response.status_code}")
        try:
            parsed = response.json()
        except ValueError as exc:
            raise SimpleFinError("SimpleFIN response was not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise SimpleFinError("SimpleFIN response was not a JSON object")
        return parsed

    def fetch_chunked(
        self, start: datetime, end: datetime, chunk_days: int = 60
    ) -> Iterator[dict[str, Any]]:
        if chunk_days <= 0:
            raise ValueError("chunk_days must be positive")
        if start >= end:
            return
        window = timedelta(days=chunk_days)
        cursor = start
        while cursor < end:
            chunk_end = min(cursor + window, end)
            logger.info(
                "Fetching SimpleFIN window %s → %s",
                cursor.date(),
                chunk_end.date(),
            )
            yield self.fetch(cursor, chunk_end)
            cursor = chunk_end
