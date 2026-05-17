class GoettaFinanceError(Exception):
    """Base for all goetta-finance errors. Caught at the CLI boundary."""


class ConfigError(GoettaFinanceError):
    """Config file missing, malformed, or unwritable."""


class SetupTokenError(GoettaFinanceError):
    """SimpleFIN setup token invalid, already claimed, or rejected by Bridge."""


class SimpleFinError(GoettaFinanceError):
    """SimpleFIN Bridge returned an error or unparseable response."""


class StoreError(GoettaFinanceError):
    """Storage backend failure (schema, query, or connection)."""
