"""A permanent record of what the trader did: every order, fill and close, with the quotes around it.

Logs and Slack messages cannot be analysed afterwards, and the book's realized P&L is gone when the bot stops. The
journal is what makes a paper or live run measurable, above all how far from the mid orders really fill, which
end-of-day data cannot show. P&L here is before commissions.
"""

import csv
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

FIELDS = (
    "time",
    "event",
    "ticker",
    "strategy",
    "action",
    "quantity",
    "expiry",
    "strike",
    "bid",
    "ask",
    "mid",
    "limit",
    "fill",
    "pnl_usd",
    "reason",
    "order_id",
    "dry_run",
)


class TradeJournal:
    """Appends one CSV row per trade event. A failing write is logged and never affects trading."""

    def __init__(self, path, dry_run=False):
        self.path = Path(path)
        self.dry_run = dry_run

    def record(self, event, spec, **fields):
        row = {
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": event,
            "ticker": spec.ticker,
            "strategy": spec.strategy,
            "action": spec.action,
            "quantity": spec.quantity,
            "expiry": spec.expiry,
            "strike": spec.strike,
            "dry_run": self.dry_run,
            **fields,
        }
        try:
            is_new = not self.path.exists() or self.path.stat().st_size == 0
            with self.path.open("a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=FIELDS, restval="")
                if is_new:
                    writer.writeheader()
                writer.writerow(row)
        except OSError:
            logger.exception("Could not write to the trade journal %s", self.path)
