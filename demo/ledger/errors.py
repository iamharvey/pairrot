"""Ledger exceptions."""


class LedgerError(Exception):
    """Base class for ledger failures."""


class InsufficientFunds(LedgerError):
    pass


class AccountFrozen(LedgerError):
    pass


class AccountClosed(LedgerError):
    pass


class NotFound(LedgerError):
    pass


class DuplicateAccount(LedgerError):
    pass


class Forbidden(LedgerError):
    pass


class GatewayError(LedgerError):
    """The card gateway failed or did not answer in time."""
