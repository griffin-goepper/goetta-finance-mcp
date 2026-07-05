"""Shared JSON-friendly value conversion for MCP tool output.

Extracted per the "rule of three with explicit defer" note that lived in
``spending_by_category.py`` once the goals tool became the next
serialization site. Money is always emitted as a string (never float);
datetimes and dates as ISO 8601.

Field-wise serializers (``tools/transactions.py``, ``tools/accounts.py``,
``tools/uncategorized.py``) build shaped dicts and intentionally do not
route through this helper.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any


def serialize_value(value: Any) -> Any:
    """Decimal -> str, datetime/date -> isoformat, everything else as-is."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value
