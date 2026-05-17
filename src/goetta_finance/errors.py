class GoettaFinanceError(Exception):
    """Base for all goetta-finance errors. Caught at the CLI boundary."""


class ConfigError(GoettaFinanceError):
    """Config file missing, malformed, or unwritable."""


class SetupTokenError(GoettaFinanceError):
    """SimpleFIN setup token invalid, already claimed, or rejected by Bridge."""


class SimpleFinError(GoettaFinanceError):
    """SimpleFIN Bridge returned an error or unparseable response."""


class BridgeAuthError(SimpleFinError):
    """Bridge rejected the access URL (HTTP 401/403). The credentials are
    likely revoked or wrong; re-run ``goetta-finance init`` to reclaim."""


class BridgeRateLimitError(SimpleFinError):
    """Bridge throttled the request (HTTP 429). Back off and retry later."""


class BridgeUnavailableError(SimpleFinError):
    """Bridge returned a 5xx. Transient on Bridge's side; retry later."""


class StoreError(GoettaFinanceError):
    """Storage backend failure (schema, query, or connection)."""
