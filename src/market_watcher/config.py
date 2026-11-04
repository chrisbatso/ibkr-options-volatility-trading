import json
import os
from dataclasses import dataclass
from typing import Any

from dotenv import dotenv_values


@dataclass
class Context:
    """Global context for MarketWatcher trading bot."""

    # Config from environment variables
    config: dict[str, Any] = None

    # MarketWatcher state path
    state_path: str = ""

    @property
    def state(self) -> dict[str, Any]:
        """Returns entire state object from json file."""
        with open(self.state_path) as f:
            state = json.load(f)
            return state

    @property
    def running(self) -> bool:
        """Reads and returns running flag from state json."""
        with open(self.state_path) as f:
            state = json.load(f)
            return state["running"]

    @running.setter
    def running(self, new_state: bool) -> None:
        """Sets the running flag in the state json."""

        old_state = self.state

        # Check if state needs updating
        if old_state["running"] == new_state:
            state = "running" if new_state else "stopped"
            raise ValueError(f"MarketWatcher bot is already {state}.")

        # Store new running flag value to state json file
        with open(self.state_path, "w") as f:
            old_state["running"] = new_state
            json.dump(obj=old_state, fp=f)


# Environment variable settings from .env files
config = {
    **dotenv_values(".env"),  # load shared development variables
    **os.environ,  # override loaded values with environment variables
}


context = Context(config=config, state_path=r"src/market_watcher/state.json")
