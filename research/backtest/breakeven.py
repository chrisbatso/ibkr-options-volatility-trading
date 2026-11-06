# ruff: noqa: E501 - report lines read better whole
"""How close to the mid must V5 sell to break even? Fill = bid + x * (mid - bid), so x = 0 is the bid and x = 1 the mid."""

from pathlib import Path

import evaluate as E
import pandas as pd

DATA = Path(__file__).parent / "data"
trades = pd.read_csv(DATA / "improve_trades.csv", parse_dates=["day", "expiry"])
rich = trades[(trades.side == "short") & (trades.hold == "expiry") & (trades.ratio >= 1.5) & (trades.bid > 0)]
print(f"V5 trades {len(rich)}; half-spread as a share of mid: median {((rich.mid - rich.bid) / rich.mid).median():.1%}")
print("x = how far from the bid towards the mid the sale fills")
for x in (0.0, 0.25, 0.5, 0.75, 1.0):
    fill = rich.bid + x * (rich.mid - rich.bid)
    usd = (fill - rich.value) * 100 - E.COMMISSION
    net = usd / (fill * 100)
    frame = rich.assign(usd=usd, net=net)
    parts = []
    for label, part in (("dev", frame[frame.day < "2025-01-01"]), ("val", frame[frame.day >= "2025-01-01"])):
        bets = part.groupby(["ticker", "expiry"]).agg(net=("net", "mean"), usd=("usd", "mean"))
        parts.append(
            f"{label}: net {part.groupby('day').net.mean().mean():+7.1%}, per bet {bets.net.mean():+7.1%}, $ per trade {part.usd.mean():+5.0f}"
        )
    no_2026 = frame[frame.day < "2026-01-01"]
    print(f"  x={x:.2f}  " + "  |  ".join(parts) + f"  |  before 2026 $ per trade {no_2026.usd.mean():+5.0f}")
print("share of total dollars at the bid from the 10 best trades:")
usd = (rich.bid - rich.value) * 100 - E.COMMISSION
print(
    f"  total {usd.sum():+,.0f}; 10 best {usd.nlargest(10).sum():+,.0f}; without them {usd.sum() - usd.nlargest(10).sum():+,.0f}"
)
