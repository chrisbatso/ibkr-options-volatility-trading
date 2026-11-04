import os
from dataclasses import dataclass, field
from typing import Any

from dotenv import dotenv_values


@dataclass
class Context:
    """Global context for the MarketWatcher trading bot."""

    # Configuration from the .env file in the working directory, overridden by environment variables.
    config: dict[str, Any] = field(default_factory=dict)


config = {
    **dotenv_values(".env"),  # shared development variables
    **os.environ,  # environment variables take precedence
}

context = Context(config=config)


_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def config_flag(key, default):
    """Returns a boolean configuration value (true/false, yes/no, on/off, 1/0), or ``default`` when it is unset."""
    value = context.config.get(key)
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ValueError(f"Configuration value {key}={value!r} is not a boolean; use true or false")


def config_int(key, default):
    """Returns an integer configuration value, or ``default`` when it is unset."""
    value = context.config.get(key)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError:
        raise ValueError(f"Configuration value {key}={value!r} is not an integer") from None


def config_float(key, default):
    """Returns a numeric configuration value as a float, or ``default`` when it is unset."""
    value = context.config.get(key)
    if value is None or not value.strip():
        return default
    try:
        return float(value)
    except ValueError:
        raise ValueError(f"Configuration value {key}={value!r} is not a number") from None
