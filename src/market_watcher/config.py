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
