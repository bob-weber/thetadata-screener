"""Shares and average cost per symbol — the one copy of that bookkeeping.

The P&L chart, the weekly RoR and the Positions tab's holdings reconciliation
all walk stock trades the same way: a buy adds shares and their cost, a sale
takes shares out at the running average cost and realizes the difference. Each
used to carry its own loop, and a fix landing in one copy and not the others is
how the charts and the tabs came to disagree.
"""


class ShareBook:
    """Running shares and total cost per symbol, average-cost basis."""

    def __init__(self):
        self.pos: dict[str, list[float]] = {}     # symbol → [shares, total cost]

    def shares(self, sym: str) -> float:
        return self.pos.get(sym, [0.0, 0.0])[0]

    def buy(self, sym: str, shares: float, cost: float) -> None:
        sh, tc = self.pos.get(sym, [0.0, 0.0])
        self.pos[sym] = [sh + shares, tc + abs(cost)]

    def sell(self, sym: str, shares: float, proceeds: float,
             transfer: bool = False) -> tuple[float, float]:
        """Take ``shares`` out; return (realized, cost of the shares removed).

        A transfer moves shares rather than selling them, so it realizes
        nothing. Selling more than is held (no basis on record) realizes the
        whole proceeds for the uncovered part, as a sale with zero cost would.
        """
        sh, tc = self.pos.get(sym, [0.0, 0.0])
        if sh <= 0:
            return (0.0 if transfer else proceeds), 0.0
        q = min(shares, sh)
        removed = tc / sh * q
        self.pos[sym] = [sh - q, tc - removed]
        return (0.0 if transfer else proceeds - removed), removed

    def apply(self, sym: str, action: str, shares: float, amount: float,
              transfer: bool = False) -> tuple[float, float]:
        """Book one trade (BOT/SOLD); return (realized, cost removed)."""
        if action == "BOT":
            self.buy(sym, shares, amount)
            return 0.0, 0.0
        return self.sell(sym, shares, amount, transfer)

    def held(self):
        """(symbol, shares, cost) for every symbol with shares on the books."""
        return [(s, sh, tc) for s, (sh, tc) in self.pos.items() if sh > 0]
