"""CryptoForge exception hierarchy."""


class CryptoForgeError(Exception):
    """Base exception for all CryptoForge errors."""


# --- Config ---
class ConfigError(CryptoForgeError):
    """Invalid or missing configuration."""


# --- Exchange ---
class ExchangeError(CryptoForgeError):
    """Exchange communication failure."""


class InsufficientBalance(ExchangeError):
    """Not enough balance to place order."""


class OrderRejected(ExchangeError):
    """Exchange rejected the order."""

    def __init__(self, reason: str, order_details: dict | None = None):
        self.reason = reason
        self.order_details = order_details or {}
        super().__init__(reason)


class RateLimitError(ExchangeError):
    """Exchange rate limit hit."""

    def __init__(self, retry_after: float = 0):
        self.retry_after = retry_after
        super().__init__(f"Rate limited, retry after {retry_after}s")


# --- Risk ---
class RiskError(CryptoForgeError):
    """Risk check failure."""


class CircuitBreakerTriggered(RiskError):
    """A circuit breaker has been activated."""

    def __init__(self, breaker_name: str, details: str = ""):
        self.breaker_name = breaker_name
        self.details = details
        super().__init__(f"Circuit breaker '{breaker_name}' triggered: {details}")


# --- Data ---
class DataError(CryptoForgeError):
    """Data pipeline error."""


class StateRecoveryError(DataError):
    """Failed to recover state after crash."""


# --- Strategy ---
class StrategyError(CryptoForgeError):
    """Strategy execution error."""
