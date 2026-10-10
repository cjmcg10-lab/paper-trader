"""Paper trading account: pretend money, real prices, realistic costs.

Every buy and sell pays a fee and slips against you, the way a real market
order would. Slippage is the base setting plus whatever it costs to work
through the live order book for the size being traded.
"""


def walk_book(side, usd, book, ref):
    """Average price paid (buy) or received (sell) for `usd` worth, walking the
    order book level by level. Without a book, a small fixed spread is assumed."""
    levels = (book or {}).get("asks" if side == "buy" else "bids") or []
    if not levels:
        return ref * (1.0003 if side == "buy" else 0.9997)
    left, cost, qty = usd, 0.0, 0.0
    for price, size in levels:
        take = min(left, price * size)
        cost += take
        qty += take / price
        left -= take
        if left <= 1e-9:
            break
    if left > 1e-9:  # order is bigger than the visible book: assume the rest fills 0.5% worse
        worst = levels[-1][0] * (1.005 if side == "buy" else 0.995)
        cost += left
        qty += left / worst
    return cost / qty


class Account:
    def __init__(self, state, cfg):
        self.s = state
        self.cfg = cfg
        self.fee = cfg["fee_pct"] / 100
        self.slip = cfg["slippage_pct"] / 100
        a = state.setdefault("account", {})
        a.setdefault("cash", float(cfg["start_balance"]))
        a.setdefault("positions", {})
        a.setdefault("trades", [])
        a.setdefault("fees", 0.0)
        a.setdefault("slippage", 0.0)
        a.setdefault("seq", 0)
        self.a = a

    # ---- valuation -------------------------------------------------------
    def equity(self, prices):
        return self.a["cash"] + sum(p["qty"] * prices.get(s, p["last"])
                                    for s, p in self.a["positions"].items())

    def invested(self, prices):
        return sum(p["qty"] * prices.get(s, p["last"]) for s, p in self.a["positions"].items())

    # ---- orders ----------------------------------------------------------
    def buy(self, sym, usd, mid, book, t, stop, take, why, snapshot):
        usd = min(usd, self.a["cash"] / (1 + self.fee) - 0.01)
        if usd < self.cfg["min_trade_usd"]:
            return None
        price = walk_book("buy", usd, book, mid) * (1 + self.slip)
        qty = usd / price
        fee = usd * self.fee
        slip_cost = max(0.0, (price - mid) * qty)
        self.a["cash"] -= usd + fee
        self.a["fees"] += fee
        self.a["slippage"] += slip_cost
        self.a["seq"] += 1
        pos = {"id": self.a["seq"], "sym": sym, "qty": qty, "entry": price, "mid_in": mid,
               "t": t, "cost": usd, "fee_in": fee, "slip_in": slip_cost, "stop": stop,
               "stop0": stop, "take": take, "risk": max(1e-9, (price - stop) * qty),
               "high": price, "last": mid, "checked": t, "why": why, "snap": snapshot,
               "trail": "initial"}
        self.a["positions"][sym] = pos
        return pos

    def sell(self, sym, mid, book, t, reason, kind, at=None, extra_slip=1.0):
        """Closes the whole position. `at` forces the price (stop or target that
        was touched between checks); otherwise sells into the live order book."""
        pos = self.a["positions"].pop(sym)
        qty = pos["qty"]
        if at is None:
            price = walk_book("sell", qty * mid, book, mid) * (1 - self.slip)
            ref = mid
        elif kind == "target":
            price, ref = at, at            # resting limit order: no slippage
        else:
            price, ref = at * (1 - self.slip * extra_slip), at
        gross = qty * price
        fee = gross * self.fee
        slip_cost = max(0.0, (ref - price) * qty)
        self.a["cash"] += gross - fee
        self.a["fees"] += fee
        self.a["slippage"] += slip_cost
        pnl = gross - fee - pos["cost"] - pos["fee_in"]
        trade = {"id": pos["id"], "sym": sym, "t_in": pos["t"], "t_out": t, "qty": qty,
                 "entry": pos["entry"], "exit": price, "cost": pos["cost"], "pnl": pnl,
                 "pct": pnl / pos["cost"] * 100, "r": pnl / pos["risk"],
                 "fees": pos["fee_in"] + fee, "slip": pos["slip_in"] + slip_cost,
                 "why_in": pos["why"], "why_out": reason, "kind": kind, "snap": pos["snap"]}
        self.a["trades"].append(trade)
        self.a["trades"] = self.a["trades"][-600:]
        return trade

    # ---- stops and targets touched since the last check -------------------
    def sweep(self, sym, c5, price, t):
        """Looks at every 5-minute candle since the last check so a stop or
        target touched between runs is not missed. If both were touched inside
        the same candle, the stop is assumed to have come first."""
        pos = self.a["positions"].get(sym)
        if not pos:
            return None
        for ct, o, h, l, c, v in c5:
            if ct <= pos["t"] or ct + 300 <= pos["checked"]:
                continue
            if l <= pos["stop"]:
                return self.sell(sym, price, None, min(t, ct + 300), self._stop_reason(pos),
                                 "stop", at=min(pos["stop"], o), extra_slip=2.0)
            if h >= pos["take"]:
                return self.sell(sym, price, None, min(t, ct + 300),
                                 "Profit target reached", "target", at=max(pos["take"], o))
            pos["high"] = max(pos["high"], h)
        pos["checked"] = t
        pos["last"] = price
        pos["high"] = max(pos["high"], price)
        if price <= pos["stop"]:
            return self.sell(sym, price, None, t, self._stop_reason(pos), "stop",
                             at=price, extra_slip=2.0)
        if price >= pos["take"]:
            return self.sell(sym, price, None, t, "Profit target reached", "target", at=price)
        return None

    @staticmethod
    def _stop_reason(pos):
        return {"initial": "Stop-loss hit",
                "breakeven": "Stopped out at breakeven after the price came back",
                "trailing": "Trailing stop hit, locking in the gain"}[pos["trail"]]

    def trail(self, sym, atr):
        """Once a trade is up by its initial risk, the stop moves to breakeven
        (after costs). Further up, it follows the price at a distance."""
        pos = self.a["positions"].get(sym)
        if not pos:
            return None
        r = (pos["entry"] - pos["stop0"])
        gain_r = (pos["high"] - pos["entry"]) / r if r > 0 else 0
        moved = None
        if gain_r >= 1.0:
            be = pos["entry"] * (1 + 2 * self.fee + 2 * self.slip)
            if pos["stop"] < be:
                pos["stop"], pos["trail"], moved = be, "breakeven", "breakeven"
        if gain_r >= 1.5 and atr:
            ts = pos["high"] - self.cfg["trail_atr_mult"] * atr
            if ts > pos["stop"] * 1.0005:
                pos["stop"], pos["trail"] = ts, "trailing"
                moved = moved or "trailing"
        return moved


def stats(account, equity_curve, start_balance):
    tr = account["trades"]
    wins = [x for x in tr if x["pnl"] > 0]
    losses = [x for x in tr if x["pnl"] <= 0]
    gw, gl = sum(x["pnl"] for x in wins), -sum(x["pnl"] for x in losses)
    peak, dd = start_balance, 0.0
    for p in equity_curve:
        peak = max(peak, p[1])
        dd = max(dd, (peak - p[1]) / peak * 100)
    hold = [(x["t_out"] - x["t_in"]) / 3600 for x in tr]
    return {
        "trades": len(tr), "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / len(tr) * 100 if tr else None,
        "profit_factor": (gw / gl if gl > 0 else None) if tr else None,
        "avg_win": gw / len(wins) if wins else None,
        "avg_loss": -gl / len(losses) if losses else None,
        "avg_r": sum(x["r"] for x in tr) / len(tr) if tr else None,
        "avg_hold_h": sum(hold) / len(hold) if hold else None,
        "max_drawdown": dd, "fees": account["fees"], "slippage": account["slippage"],
        "realized": sum(x["pnl"] for x in tr),
        "best": max((x["pnl"] for x in tr), default=None),
        "worst": min((x["pnl"] for x in tr), default=None),
    }
