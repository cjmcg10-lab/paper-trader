"""One trading cycle: gather data, analyse, decide, record. Run every 5 minutes.

    python3 bot.py --data ./data

Reads and writes two files in the data folder:
  state.json      the bot's memory (account, positions, caches)
  dashboard.json  everything the phone dashboard shows
"""
import argparse
import json
import os
import sys
import traceback
from datetime import datetime, timezone

import brain
import feeds
import paper
from feeds import now

DEFAULTS = {
    "start_balance": 10000, "coins": 5, "timezone": "America/New_York",
    "fee_pct": 0.20, "slippage_pct": 0.05,
    "enter_score": 0.25, "exit_score": -0.15, "min_confidence": 0.45,
    "risk_per_trade_pct": 0.75, "max_position_pct": 25, "max_exposure_pct": 100,
    "stop_atr_mult": 2.5, "min_stop_pct": 1.2, "max_stop_pct": 5.0, "target_r": 2.2,
    "trail_atr_mult": 1.5, "max_hold_hours": 48, "daily_loss_limit_pct": 3.0,
    "cooldown_min": 30, "cooldown_after_stop_min": 90, "loss_streak_pause": 3,
    "loss_streak_pause_hours": 3, "event_blackout_before_min": 45,
    "event_blackout_after_min": 20, "max_spread_bps": 15, "min_trade_usd": 50,
    "ai_daily_tokens": 160000, "ai_daily_calls": 110, "reset_counter": 0,
}
HERE = os.path.dirname(os.path.abspath(__file__))


def load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, separators=(",", ":"))
    os.replace(tmp, path)


def load_config():
    cfg = dict(DEFAULTS)
    try:
        user = load_json(os.path.join(HERE, "config.json"))
        cfg.update({k: v for k, v in user.items() if k in DEFAULTS and v is not None})
    except Exception as e:
        print("config.json not used (%s); running on defaults" % e)
    return cfg


def fresh_state(cfg):
    return {"v": 1, "started": now(), "runs": 0, "reset": cfg["reset_counter"], "cache": {},
            "memo": {}, "news": [], "feed": [], "equity": [], "bench": None, "day": {},
            "cool": {}, "stance": {}, "venue": {}, "last_price": {}, "pause_until": 0,
            "brief": None, "llm": {"day": "", "calls": 0, "tokens": 0, "last": 0}}


def load_state(data_dir, cfg):
    path = os.path.join(data_dir, "state.json")
    state = None
    if os.path.exists(path):
        try:
            state = load_json(path)
        except Exception:
            state = load_json(path + ".bak")       # last good copy; raises if that is broken too
    if state is None or state.get("reset") != cfg["reset_counter"]:
        state = fresh_state(cfg)
    for k, v in fresh_state(cfg).items():
        state.setdefault(k, v)
    return state


def local_day(cfg, t):
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(t, ZoneInfo(cfg["timezone"])).strftime("%Y-%m-%d")
    except Exception:
        return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")


def usd(x, cents=True):
    return ("-" if x < 0 else "") + ("${:,.2f}" if cents else "${:,.0f}").format(abs(x))


def px(x):
    return "${:,.2f}".format(x) if x >= 100 else "${:,.3f}".format(x) if x >= 1 else "${:,.5f}".format(x)


def say(state, kind, sym, title, text, t=None):
    state["feed"].append({"t": int(t or now()), "kind": kind, "sym": sym, "title": title, "text": text})
    state["feed"] = state["feed"][-300:]


# ---------------------------------------------------------------------------
def gather(state, cfg, H):
    """Fetches every source at once. Returns one dict of raw material."""
    uni, _ = feeds.cached(state, H, "universe", "Coin rankings", "Markets", 86400,
                          lambda: feeds.fetch_universe(cfg["coins"]))
    uni = list(uni or feeds.FALLBACK_UNIVERSE[:cfg["coins"]])
    known = {c["sym"] for c in uni}
    for s in state["account"]["positions"] if "account" in state else []:
        if s not in known:                      # still holding a coin that left the top list
            uni.append({"sym": s, "name": s})
    syms = [c["sym"] for c in uni]

    tasks = {}
    for s in syms:
        tasks["mkt:" + s] = lambda s=s: H.run("Prices: " + s, "Markets", feeds.fetch_market,
                                               s, state["venue"].get(s))
        tasks["okx:" + s] = lambda s=s: feeds.cached(
            state, H, "okx:" + s, "Funding rate: " + s, "Futures", 600,
            lambda: feeds.fetch_okx_funding(s))
        tasks["cm:" + s] = lambda s=s: feeds.cached(
            state, H, "cm:" + s, "Network activity: " + s, "On-chain", 3 * 3600,
            lambda: feeds.fetch_coinmetrics(s))
        if s in feeds.CHAINS:
            tasks["dex:" + s] = lambda s=s: feeds.cached(
                state, H, "dex:" + s, "On-chain trading: " + s, "On-chain", 3 * 3600,
                lambda: feeds.fetch_dex_volume(feeds.CHAINS[s]))
    tasks["hl"] = lambda: H.run("Futures positioning", "Futures", feeds.fetch_hyperliquid, syms)
    for name, kind, url in feeds.NEWS_FEEDS:
        tasks["news:" + name] = lambda a=(name, kind, url): H.run(a[0], "News", feeds.fetch_feed, *a)
    for name, kind, url in feeds.SOCIAL_FEEDS:
        tasks["soc:" + name] = lambda a=(name, kind, url): feeds.cached(
            state, H, "soc:" + a[0], a[0], "Crowd mood", 900, lambda: feeds.fetch_feed(*a))
    tasks["fng"] = lambda: feeds.cached(state, H, "fng", "Fear & Greed index", "Crowd mood", 3600,
                                        feeds.fetch_fear_greed)
    tasks["macro"] = lambda: feeds.cached(state, H, "macro", "Stocks, dollar, rates", "Economy",
                                          900, feeds.fetch_macro)
    tasks["cal"] = lambda: feeds.cached(state, H, "cal", "Economic calendar", "Economy",
                                        12 * 3600, feeds.fetch_calendar)
    tasks["stable"] = lambda: feeds.cached(state, H, "stable", "Stablecoin supply", "On-chain",
                                           3 * 3600, feeds.fetch_stablecoins)
    raw = feeds.parallel(tasks, 12)
    raw["universe"] = uni
    return raw


def read_news(state, cfg, raw, uni, held):
    """Collects headlines and scores the new ones. Returns the engine in use."""
    fresh = []
    for k, v in raw.items():
        if k.startswith("news:") and v:
            fresh += v
        elif k.startswith("soc:") and v and v[0]:
            fresh += v[0]
    brain.merge_headlines(state, [dict(x) for x in fresh], uni)
    t, L = now(), state["llm"]
    day = local_day(cfg, t)
    if L.get("day") != day:
        L.update({"day": day, "calls": 0, "tokens": 0})
    pending = [x for x in state["news"] if x["s"] is None]
    provider = feeds.llm_provider()
    syms = [c["sym"] for c in uni]
    if provider and pending:
        urgent = len(pending) >= 5 or any(set(x["a"]) & held for x in pending)
        budget = L["calls"] < cfg["ai_daily_calls"] and L["tokens"] < cfg["ai_daily_tokens"]
        if budget and (urgent or t - L.get("last", 0) >= 600):
            batch = sorted(pending, key=lambda x: (x["kind"] == "social", -x["t"]))[:20]
            lines = "\n".join("%d. [%s] %s" % (i, x["src"], x["title"]) for i, x in enumerate(batch))
            L["last"] = t
            L["calls"] += 1
            try:
                ans, used = feeds.llm_json(state, brain.SYSTEM_PROMPT % ", ".join(syms), lines)
                L["tokens"] += used
                n = brain.apply_ai_scores(batch, ans, syms)
                if not n:
                    raise ValueError("answer had no usable scores")
                L["ok"], L["err"] = True, ""
                if isinstance(ans.get("brief"), str) and ans["brief"].strip():
                    state["brief"] = {"t": int(t), "text": ans["brief"].strip()[:400]}
                big = [x for x in batch if x.get("by") == "ai" and x["m"] >= 0.8
                       and abs(x["s"]) >= 0.3 and x["a"]]
                for x in sorted(big, key=lambda x: -x["m"] * abs(x["s"]))[:1]:
                    say(state, "news", None, "Big headline", "“%s” Judged %s for %s%s." % (
                        x["title"], "bullish" if x["s"] > 0 else "bearish",
                        brain.join(["the whole market" if a == "MARKET" else a for a in x["a"]]),
                        ": " + x["w"] if x["w"] else ""))
            except Exception as e:
                L["ok"], L["err"] = False, feeds.err_text(e)
    # anything the AI has not got to within 30 minutes is read by keywords instead
    late = [x for x in state["news"] if x["s"] is None and
            (not provider or L.get("ok") is False or t - x["t"] > 1800)]
    brain.apply_keyword_scores(late)
    if not provider:
        return "keywords", "No AI key added, so headlines are read by keyword matching"
    if L.get("ok") is False:
        return "keywords", "AI reader unavailable (%s); using keyword matching for now" % L.get("err", "")
    return "ai", "%s · %s" % (provider["name"], L.get("model") or "connecting")


def analyse(state, cfg, raw, engine):
    """Builds the six-family view for every coin."""
    uni = raw["universe"]
    hl = raw.get("hl") or {}
    fng = (raw.get("fng") or (None, None))[0]
    macro, macro_age = raw.get("macro") or (None, None)
    macro_fam = brain.macro_family(macro, macro_age)
    stable = raw.get("stable") if (raw.get("stable") or (None,))[0] else None
    views = {}
    for c in uni:
        s = c["sym"]
        m = raw.get("mkt:" + s)
        if not m:
            continue
        try:
            state["venue"][s] = m["venue"]
            c5, c60 = m["c5"], m["c60"]
            price = c5[-1][4]
            reg, strength = brain.regime(c60)
            pa, atr = brain.price_action(c5, c60, reg)
            aprs = [x for x in [(hl.get(s) or {}).get("apr"),
                                ((raw.get("okx:" + s) or (None,))[0] or {}).get("apr")] if x is not None]
            deriv = {"apr": sum(aprs) / len(aprs) if aprs else None, "oi": (hl.get(s) or {}).get("oi")}
            cm = raw.get("cm:" + s)
            dex = raw.get("dex:" + s)
            fams = [pa, brain.order_flow(s, c5, m["book"], deriv, state),
                    brain.news_family(s, state["news"], engine),
                    brain.mood_family(s, fng, state["news"]),
                    brain.onchain_family(s, cm if cm and cm[0] else None, stable,
                                         dex if dex and dex[0] else None),
                    dict(macro_fam)]
            views[s] = {"sym": s, "name": c.get("name") or s, "price": price, "reg": reg,
                        "atr": atr, "fams": fams, "book": m["book"], "c5": c5, "venue": m["venue"],
                        "chg24": c60[-1][4] / c60[-25][4] - 1, "chg1h": c5[-1][4] / c5[-13][4] - 1,
                        "spark": [round(r[4], 6) for r in c5[-288::6]], "stale": now() - c5[-1][0] > 1200}
        except Exception as e:
            print("analysis failed for", s, repr(e))
    # Bitcoin leads the market: its price action colours every other coin a little
    btc = views.get("BTC")
    for s, v in views.items():
        if btc and s != "BTC":
            p = v["fams"][0]
            p["score"] = round(0.8 * p["score"] + 0.2 * btc["fams"][0]["score"], 3)
        v["score"], v["conf"] = brain.combine(v["fams"], v["reg"])
    return views


def next_event(raw, cfg):
    cal = (raw.get("cal") or (None,))[0] or {}
    t = now()
    upcoming = sorted((e for e in cal.get("events", []) if e["t"] > t - cfg["event_blackout_after_min"] * 60),
                      key=lambda e: e["t"])
    blackout = next((e for e in upcoming if e["t"] - cfg["event_blackout_before_min"] * 60 <= t), None)
    return upcoming[:4], blackout


def trade(state, cfg, views, acct, blackout):
    """Applies the rules: exits first, then entries, strongest signal first."""
    t = now()
    A = acct.a
    prices = {s: v["price"] for s, v in views.items()}
    state["last_price"].update(prices)
    allp = state["last_price"]
    stances = {}

    def closed(tr):
        s = tr["sym"]
        won = tr["pnl"] > 0
        state["cool"][s] = t + 60 * (cfg["cooldown_after_stop_min"] if tr["kind"] == "stop" and not won
                                     else cfg["cooldown_min"])
        say(state, "sell", s, "Sold %s: %s" % (s, "profit " + usd(tr["pnl"]) if won else "loss " + usd(-tr["pnl"])),
            "%s. Sold at %s (bought at %s), %+.2f%% after %s in fees and slippage."
            % (tr["why_out"], px(tr["exit"]), px(tr["entry"]), tr["pct"], usd(tr["fees"] + tr["slip"])),
            tr["t_out"])
        streak = 0
        for x in reversed(A["trades"]):
            if x["pnl"] > 0:
                break
            streak += 1
        if streak and streak % cfg["loss_streak_pause"] == 0:
            state["pause_until"] = t + cfg["loss_streak_pause_hours"] * 3600
            say(state, "warn", None, "Pausing new trades",
                "%d losing trades in a row. Taking a %d-hour break from opening new positions; "
                "existing ones are still managed." % (streak, cfg["loss_streak_pause_hours"]))
        stances[s] = {"action": "SOLD", "text": "Just sold: %s. %s %s on this trade." % (
            tr["why_out"].lower(), "Made" if won else "Lost", usd(abs(tr["pnl"])))}

    # 1. stops and targets touched since the last check; then trail stops
    for s in list(A["positions"]):
        v = views.get(s)
        if not v:
            continue
        tr = acct.sweep(s, v["c5"], v["price"], t)
        if tr:
            closed(tr)
            continue
        before = A["positions"][s]["trail"]
        acct.trail(s, v["atr"])
        moved = A["positions"][s]["trail"]
        if moved != before:                     # announce each stage once, not every nudge
            p = A["positions"][s]
            say(state, "info", s, "Stop raised on " + s,
                "Price moved in our favour, so the stop is now at %s (%s). %s" % (
                    px(p["stop"]), "breakeven" if moved == "breakeven" else "trailing below the high",
                    "This trade can no longer lose money beyond costs." if moved == "breakeven"
                    else "Part of the gain is now locked in."))

    # 2. day bookkeeping and account-level brakes
    eq = acct.equity(allp)
    day = local_day(cfg, t)
    if state["day"].get("date") != day:
        if state["day"].get("date"):
            d = eq - state["day"]["start"]
            say(state, "info", None, "Day closed " + ("up " if d >= 0 else "down ") + usd(abs(d)),
                "Account finished %s at %s." % (state["day"]["date"], usd(eq)))
        state["day"] = {"date": day, "start": eq, "limit_hit": False}
    day_pct = (eq / state["day"]["start"] - 1) * 100
    if day_pct <= -cfg["daily_loss_limit_pct"] and not state["day"]["limit_hit"]:
        state["day"]["limit_hit"] = True
        say(state, "warn", None, "Daily loss limit reached",
            "Down %.1f%% today. No new trades until tomorrow; open positions keep their stops." % -day_pct)
    brake = None
    if state["day"]["limit_hit"]:
        brake = ("limit", "the daily loss limit (%.0f%%) was reached, so no new trades today"
                 % cfg["daily_loss_limit_pct"])
    elif t < state["pause_until"]:
        brake = ("streak", "new trades are paused for %.1f more hours after a run of losses"
                 % ((state["pause_until"] - t) / 3600))
    elif blackout:
        mins = (blackout["t"] - t) / 60
        brake = ("event", "a major US announcement (%s) %s, and prices can jump either way around it"
                 % (blackout["title"], "is due in %d min" % mins if mins > 0 else "just came out"))

    btc = views.get("BTC")
    market_weak = bool(btc and btc["reg"] == "downtrend" and btc["fams"][0]["score"] < -0.3)

    for s, v in sorted(views.items(), key=lambda kv: -kv[1]["score"]):
        if s in stances:
            continue
        fams, score, conf, price = v["fams"], v["score"], v["conf"], v["price"]
        up, down = brain.drivers(fams, 1), brain.drivers(fams, -1)
        thr = cfg["enter_score"] + (0.08 if v["reg"] == "volatile" else 0) + \
            (0.08 if market_weak and s != "BTC" else 0)
        v["thr"] = round(thr, 2)
        pos = A["positions"].get(s)

        # ---- already holding: look for a reason to get out ----
        if pos:
            held_h = (t - pos["t"]) / 3600
            gain_r = (price - pos["entry"]) * pos["qty"] / pos["risk"]
            why = None
            if v["stale"]:
                pass
            elif score <= cfg["exit_score"]:
                why = "Signals turned negative (%s)" % (brain.join(down) if down else "overall score %.2f" % score)
            elif fams[0]["score"] <= -0.4 and v["reg"] == "downtrend":
                why = "The price trend broke down"
            elif held_h >= cfg["max_hold_hours"] and gain_r < 0.5:
                why = "Held %d hours without real progress, freeing up the money" % held_h
            if why:
                closed(acct.sell(s, price, v["book"], t, why, "signal"))
                continue
            pnl = (price - pos["entry"]) * pos["qty"] - pos["fee_in"]
            stances[s] = {"action": "HOLDING", "text":
                          "Holding. %s %s (%+.2f%%) since buying %s ago. Signal is %+.2f; it sells at %+.2f "
                          "or if the stop at %s is hit." % (
                              "Up" if pnl >= 0 else "Down", usd(abs(pnl)), pnl / pos["cost"] * 100,
                              "%dh" % held_h if held_h >= 1 else "%d min" % (held_h * 60), score,
                              cfg["exit_score"], px(pos["stop"]))}
            continue

        # ---- not holding: is the case strong enough to buy? ----
        pull = ("%s pulling up" % brain.join(up).capitalize() if up else "") + \
               ("; " if up and down else "") + \
               ("%s pulling down" % (brain.join(down) if up else brain.join(down).capitalize()) if down else "")
        pull = (pull + ".") if pull else "Nothing is pulling strongly either way."
        if v["stale"]:
            stances[s] = {"action": "WATCHING", "text": "Price data is delayed, so not trading this coin right now."}
        elif score < thr:
            stances[s] = {"action": "WATCHING", "text":
                          "Watching. Overall signal is %+.2f and it buys at %+.2f or higher. %s"
                          % (score, thr, pull)}
        elif conf < cfg["min_confidence"]:
            stances[s] = {"action": "WATCHING", "text":
                          "Signal is high enough (%+.2f) but the inputs disagree with each other "
                          "(confidence %d%%, needs %d%%). %s" % (score, conf * 100, cfg["min_confidence"] * 100, pull)}
        elif fams[0]["score"] < 0.05:
            stances[s] = {"action": "WATCHING", "text":
                          "Signal is positive (%+.2f) but the price itself has not started rising yet, "
                          "so waiting for that confirmation. %s" % (score, pull)}
        else:
            spread = fams[1]["detail"].get("spread_bps")
            block = brake
            if not block and t < state["cool"].get(s, 0):
                block = ("cool", "it sold this coin a short while ago and waits %d more minutes "
                                 "before buying it back" % ((state["cool"][s] - t) / 60 + 1))
            if not block and spread is not None and spread > cfg["max_spread_bps"]:
                block = ("spread", "the gap between buy and sell prices is unusually wide right now")
            stop_pct = brain.clamp(cfg["stop_atr_mult"] * v["atr"] / price,
                                   cfg["min_stop_pct"] / 100, cfg["max_stop_pct"] / 100)
            conviction = brain.clamp(0.7 + (score - thr) / 0.25 * 0.5, 0.7, 1.3) * \
                (0.5 if v["reg"] == "volatile" else 1)
            eq = acct.equity(allp)
            size = min(eq * cfg["risk_per_trade_pct"] / 100 * conviction / stop_pct,
                       eq * cfg["max_position_pct"] / 100,
                       eq * cfg["max_exposure_pct"] / 100 - acct.invested(allp),
                       A["cash"] / (1 + acct.fee) - 0.01)
            if not block and size < cfg["min_trade_usd"]:
                block = ("cash", "the account is already fully invested")
            if block:
                stances[s] = {"action": "HELD BACK", "key": block[0], "text":
                              "Signal is strong enough to buy (%+.2f), but %s." % (score, block[1])}
                if (state["stance"].get(s) or {}).get("key") != block[0]:
                    say(state, "warn", s, "Skipped a buy on " + s, stances[s]["text"])
                continue
            mid = price
            if v["book"]:
                mid = (v["book"]["bids"][0][0] + v["book"]["asks"][0][0]) / 2
            est = mid * (1 + acct.slip)
            stop = est * (1 - stop_pct)
            take = est * (1 + stop_pct * cfg["target_r"])
            why = ("%s point%s up" % (brain.join(up).capitalize(), "s" if len(up) == 1 else "")
                   if up else "Overall signal is positive")
            snap = {"score": score, "conf": conf, "reg": v["reg"],
                    "fams": {x["key"]: x["score"] for x in fams}}
            pos = acct.buy(s, size, mid, v["book"], t, stop, take, why, snap)
            if not pos:
                continue
            risk = (pos["entry"] - stop) * pos["qty"] + 2 * pos["fee_in"]
            text = ("Bought %s at %s because %s%s. Stop-loss at %s (-%.1f%%), profit target %s (+%.1f%%). "
                    "Most it should lose: about %s (%.2f%% of the account)." % (
                        usd(pos["cost"], False), px(pos["entry"]), why[0].lower() + why[1:],
                        "; " + brain.join(down) + (" is" if len(down) == 1 else " are") + " the main worry"
                        if down else " and nothing major disagrees",
                        px(stop), stop_pct * 100, px(take), stop_pct * cfg["target_r"] * 100,
                        usd(risk, False), risk / eq * 100))
            say(state, "buy", s, "Bought " + s, text)
            stances[s] = {"action": "BOUGHT", "text": text}
    state["stance"] = {s: {"action": x["action"], "key": x.get("key")} for s, x in stances.items()}
    return stances, brake


def track(state, cfg, acct):
    """Records account value next to what simply holding the coins would be worth."""
    t = now()
    P = state["last_price"]
    if state["bench"] is None:
        first = [c for c in state["cache"].get("universe", {}).get("v") or feeds.FALLBACK_UNIVERSE][:cfg["coins"]]
        if all(c["sym"] in P for c in first):
            each = cfg["start_balance"] / len(first) * (1 - acct.fee - acct.slip)
            state["bench"] = {c["sym"]: each / P[c["sym"]] for c in first}
    bench = sum(u * P[s] for s, u in state["bench"].items()) if state["bench"] else cfg["start_balance"]
    eq = acct.equity(P)
    curve = state["equity"]
    curve.append([int(t), round(eq, 2), round(bench, 2)])
    recent = [p for p in curve if t - p[0] <= 36 * 3600]
    old, seen = [], set()
    for p in reversed([p for p in curve if t - p[0] > 36 * 3600]):
        if p[0] // 3600 not in seen:        # older history thins out to one point an hour
            seen.add(p[0] // 3600)
            old.append(p)
    state["equity"] = (old[::-1] + recent)[-5000:]
    return eq, bench


def build_dashboard(state, cfg, views, stances, acct, H, engine, engine_note, events, brake, eq, bench):
    t = now()
    A = acct.a
    P = state["last_price"]
    start = cfg["start_balance"]
    coins = []
    for s, v in views.items():
        pos = A["positions"].get(s)
        coins.append({
            "sym": s, "name": v["name"], "price": v["price"], "chg24": round(v["chg24"] * 100, 2),
            "chg1h": round(v["chg1h"] * 100, 2), "spark": v["spark"], "regime": v["reg"],
            "regime_label": brain.REGIME_LABEL[v["reg"]], "score": v["score"], "conf": v["conf"],
            "thr": v.get("thr", cfg["enter_score"]), "exit": cfg["exit_score"], "venue": v["venue"],
            "stance": stances.get(s, {"action": "WATCHING", "text": ""}),
            "families": [{k: x[k] for k in ("key", "label", "score", "conf", "weight", "pull", "notes")}
                         for x in v["fams"]],
            "held": bool(pos)})
    coins.sort(key=lambda c: (not c["held"], -c["score"]))
    positions = []
    for s, p in A["positions"].items():
        price = P.get(s, p["last"])
        positions.append({
            "sym": s, "qty": p["qty"], "entry": p["entry"], "price": price, "value": p["qty"] * price,
            "cost": p["cost"], "pnl": (price - p["entry"]) * p["qty"] - p["fee_in"],
            "pct": ((price - p["entry"]) * p["qty"] - p["fee_in"]) / p["cost"] * 100,
            "stop": p["stop"], "take": p["take"],
            "stop0": p["stop0"], "trail": p["trail"], "t": int(p["t"]), "why": p["why"]})
    day_start = state["day"].get("start", start)
    holding = [p["sym"] for p in positions]
    watching = [c["sym"] for c in coins if not c["held"]]
    if brake:
        headline = "Not opening new trades: " + brake[1] + "."
    elif holding:
        headline = "Holding %s." % brain.join(holding)
    else:
        headline = "Fully in cash, waiting for a strong enough signal."
    best = max((c for c in coins if not c["held"]), key=lambda c: c["score"], default=None)
    if best and not brake:
        headline += " Closest to a buy: %s at %+.2f (needs %+.2f)." % (best["sym"], best["score"], best["thr"]) \
            if best["score"] < best["thr"] else ""
    news = [{k: x.get(k) for k in ("t", "title", "src", "url", "a", "s", "m", "w", "n", "kind", "by")}
            for x in state["news"] if x["s"] is not None and (x["m"] or 0) >= 0.15][:50]
    for x in news:
        x["t"] = int(x["t"])
    return {
        "v": 1, "updated": int(t), "started": int(state["started"]), "runs": state["runs"],
        "summary": {
            "equity": round(eq, 2), "cash": round(A["cash"], 2), "invested": round(eq - A["cash"], 2),
            "start": start, "pnl": round(eq - start, 2), "pnl_pct": round((eq / start - 1) * 100, 3),
            "bench": round(bench, 2), "bench_pct": round((bench / start - 1) * 100, 3),
            "day": round(eq - day_start, 2), "day_pct": round((eq / day_start - 1) * 100, 3),
            "status": "paused" if brake else "active", "headline": headline,
            "holding": holding, "watching": watching},
        "brief": state["brief"], "engine": {"mode": engine, "note": engine_note},
        "events": [{"t": int(e["t"]), "title": e["title"]} for e in events],
        "coins": coins, "positions": positions,
        "trades": [{k: (int(v) if k in ("t_in", "t_out") else v) for k, v in x.items() if k != "snap"}
                   for x in reversed(A["trades"][-200:])],
        "feed": list(reversed(state["feed"][-150:])),
        "equity": state["equity"],
        "stats": paper.stats(A, state["equity"], start),
        "news": news, "health": H.listing(),
        "rules": {k: cfg[k] for k in (
            "fee_pct", "slippage_pct", "enter_score", "exit_score", "min_confidence",
            "risk_per_trade_pct", "max_position_pct", "min_stop_pct", "max_stop_pct", "target_r",
            "max_hold_hours", "daily_loss_limit_pct", "cooldown_min", "loss_streak_pause",
            "event_blackout_before_min", "start_balance")},
    }


def run(data_dir):
    cfg = load_config()
    os.makedirs(data_dir, exist_ok=True)
    state = load_state(data_dir, cfg)
    acct = paper.Account(state, cfg)
    first = state["runs"] == 0
    state["runs"] += 1
    H = feeds.Health()

    raw = gather(state, cfg, H)
    engine, engine_note = read_news(state, cfg, raw, raw["universe"], set(acct.a["positions"]))
    views = analyse(state, cfg, raw, engine)
    events, blackout = next_event(raw, cfg)
    if first:
        say(state, "info", None, "Bot started",
            "Paper account opened with %s. Trading %s. Fees %.2f%% and slippage %.2f%% per trade." % (
                usd(cfg["start_balance"], False), brain.join([c["sym"] for c in raw["universe"]]),
                cfg["fee_pct"], cfg["slippage_pct"]))
    if not views:
        raise RuntimeError("no price data from any exchange this run")
    stances, brake = trade(state, cfg, views, acct, blackout)
    eq, bench = track(state, cfg, acct)
    dash = build_dashboard(state, cfg, views, stances, acct, H, engine, engine_note, events,
                           brake, eq, bench)
    spath = os.path.join(data_dir, "state.json")
    if os.path.exists(spath):
        os.replace(spath, spath + ".bak")
    save_json(spath, state)
    save_json(os.path.join(data_dir, "dashboard.json"), dash)
    print("run %d ok: equity %.2f, %d positions, %d/%d sources up, news engine %s" % (
        state["runs"], eq, len(acct.a["positions"]),
        sum(1 for h in H.items.values() if h["ok"]), len(H.items), engine))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.join(HERE, "data"))
    args = ap.parse_args()
    try:
        run(args.data)
    except Exception as e:
        # Never leave half-written files. Note the problem on the dashboard and try again next time.
        traceback.print_exc()
        try:
            path = os.path.join(args.data, "dashboard.json")
            dash = load_json(path)
            dash["error"] = {"t": int(now()), "text": feeds.err_text(e)}
            save_json(path, dash)
        except Exception:
            pass
        sys.exit(0)


if __name__ == "__main__":
    main()
