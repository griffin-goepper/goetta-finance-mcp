from __future__ import annotations

import base64
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
import respx

from goetta_finance.errors import SetupTokenError, SimpleFinError
from goetta_finance.simplefin import (
    SimpleFinClient,
    parse_accounts,
    parse_transactions,
)


def test_parse_accounts_maps_fields(demo_response: dict) -> None:
    accounts = parse_accounts(demo_response)
    assert len(accounts) == 2

    chk = next(a for a in accounts if a.id == "ACT-CHK-1")
    assert chk.org_name == "Chase"
    assert chk.name == "Checking 1234"
    assert chk.currency == "USD"
    assert chk.balance == Decimal("2543.21")
    assert chk.available_balance == Decimal("2500.00")
    assert chk.balance_date == datetime.fromtimestamp(1747929600, tz=UTC)

    vg = next(a for a in accounts if a.id == "ACT-VG-1")
    assert vg.available_balance is None
    assert vg.extra == {"yield": 0.0312}


def test_parse_transactions_drops_pending(demo_response: dict) -> None:
    txns = parse_transactions(demo_response)
    ids = {t.id for t in txns}
    assert "TX-3-PENDING" not in ids
    assert ids == {"TX-1", "TX-2", "TX-VG-1"}
    for t in txns:
        assert t.pending is False


def test_parse_transactions_amounts_are_decimal(demo_response: dict) -> None:
    txns = parse_transactions(demo_response)
    tx1 = next(t for t in txns if t.id == "TX-1")
    assert tx1.amount == Decimal("-42.18")
    assert isinstance(tx1.amount, Decimal)
    assert tx1.transacted_at == datetime.fromtimestamp(1747751400, tz=UTC)


def test_parse_transactions_missing_transacted_at_is_none(demo_response: dict) -> None:
    txns = parse_transactions(demo_response)
    tx2 = next(t for t in txns if t.id == "TX-2")
    assert tx2.transacted_at is None


@respx.mock
def test_claim_returns_access_url() -> None:
    claim_url = "https://example.invalid/claim/abc123"
    token = base64.b64encode(claim_url.encode("ascii")).decode("ascii")
    access_url = "https://user:pass@beta-bridge.simplefin.org/simplefin"
    respx.post(claim_url).mock(return_value=httpx.Response(200, text=access_url))

    assert SimpleFinClient.claim(token) == access_url


@respx.mock
def test_claim_rejects_already_claimed_token() -> None:
    claim_url = "https://example.invalid/claim/xxx"
    token = base64.b64encode(claim_url.encode("ascii")).decode("ascii")
    respx.post(claim_url).mock(return_value=httpx.Response(403, text="claimed"))

    with pytest.raises(SetupTokenError):
        SimpleFinClient.claim(token)


def test_claim_rejects_garbage_token() -> None:
    with pytest.raises(SetupTokenError):
        SimpleFinClient.claim("not-base64!!")


def test_claim_rejects_non_url_decoded_token() -> None:
    token = base64.b64encode(b"hello world").decode("ascii")
    with pytest.raises(SetupTokenError):
        SimpleFinClient.claim(token)


@respx.mock
def test_fetch_includes_basic_auth_and_dates() -> None:
    route = respx.get("https://bridge.example.invalid/simplefin/accounts").mock(
        return_value=httpx.Response(200, json={"errors": [], "accounts": []})
    )
    client = SimpleFinClient("https://user:pass@bridge.example.invalid/simplefin")
    start = datetime(2026, 5, 1, tzinfo=UTC)
    end = datetime(2026, 5, 10, tzinfo=UTC)
    result = client.fetch(start, end)
    assert result == {"errors": [], "accounts": []}

    sent = route.calls.last.request
    assert sent.url.params["start-date"] == str(int(start.timestamp()))
    assert sent.url.params["end-date"] == str(int(end.timestamp()))
    assert sent.url.params["pending"] == "0"
    auth_header = sent.headers["authorization"]
    expected = base64.b64encode(b"user:pass").decode("ascii")
    assert auth_header == f"Basic {expected}"


@respx.mock
def test_fetch_raises_on_http_error() -> None:
    respx.get("https://bridge.example.invalid/simplefin/accounts").mock(
        return_value=httpx.Response(503, text="busy")
    )
    client = SimpleFinClient("https://user:pass@bridge.example.invalid/simplefin")
    with pytest.raises(SimpleFinError):
        client.fetch(
            datetime(2026, 5, 1, tzinfo=UTC),
            datetime(2026, 5, 2, tzinfo=UTC),
        )


@respx.mock
def test_fetch_chunked_splits_200_day_window_at_60_days() -> None:
    route = respx.get("https://bridge.example.invalid/simplefin/accounts").mock(
        return_value=httpx.Response(200, json={"errors": [], "accounts": []})
    )
    client = SimpleFinClient("https://user:pass@bridge.example.invalid/simplefin")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 7, 20, tzinfo=UTC)  # 200 days
    chunks = list(client.fetch_chunked(start, end, chunk_days=60))
    assert len(chunks) == 4

    windows: list[tuple[int, int]] = []
    for call in route.calls:
        sd = int(call.request.url.params["start-date"])
        ed = int(call.request.url.params["end-date"])
        windows.append((sd, ed))
        assert ed - sd <= 60 * 86400

    assert windows[0][0] == int(start.timestamp())
    assert windows[-1][1] == int(end.timestamp())
    for i in range(len(windows) - 1):
        assert windows[i][1] == windows[i + 1][0]


def test_fetch_chunked_empty_window_yields_nothing() -> None:
    client = SimpleFinClient("https://user:pass@bridge.example.invalid/simplefin")
    t = datetime(2026, 5, 1, tzinfo=UTC)
    assert list(client.fetch_chunked(t, t)) == []
