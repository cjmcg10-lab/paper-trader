"""Data collection: everything the bot reads from the outside world.

Every source is free and needs no account. Each one is fetched independently,
so a source that is down, slow or blocked never stops the run. It just shows
up as unavailable on the dashboard and that part of the analysis counts less.
"""
import gzip
import hashlib
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36")

_clock = time.time


def now():
    return _clock()


def http(url, data=None, headers=None, timeout=12, raw=False, limit=6_000_000):
    """GET (or POST when data is given). Returns parsed JSON, or text if raw."""
    h = {"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "gzip"}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=body, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        b = r.read(limit)
        if r.headers.get("Content-Encoding") == "gzip" or b[:2] == b"\x1f\x8b":
            b = gzip.decompress(b)
    text = b.decode("utf-8", "replace")
    return text if raw else json.loads(text)


def f(x, default=None):
    try:
        v = float(x)
        return v if v == v and abs(v) != float("inf") else default
    except (TypeError, ValueError):
        return default


def err_text(e):
    if isinstance(e, urllib.error.HTTPError):
        return {403: "blocked (403)", 429: "rate limited (429)", 451: "not available here (451)",
                404: "not found (404)"}.get(e.code, "HTTP %s" % e.code)
    if isinstance(e, (urllib.error.URLError, TimeoutError)):
        return "no response"
    return (type(e).__name__ + ": " + str(e))[:70]


class Health:
    """Remembers which sources answered this run, for the dashboard."""

    def __init__(self):
        self.items = {}

    def set(self, name, group, ok, note="", age=0):
        self.items[name] = {"name": name, "group": group, "ok": bool(ok),
                            "note": note, "age": int(age or 0)}

    def run(self, name, group, fn, *a):
        try:
            v = fn(*a)
            self.set(name, group, True)
            return v
        except Exception as e:  # any source may fail; never let it stop the run
            self.set(name, group, False, err_text(e))
            return None

    def listing(self):
        return sorted(self.items.values(), key=lambda x: (x["group"], x["name"]))


def cached(state, H, key, name, group, ttl, fn):
    """Slow-moving data is refetched only every `ttl` seconds. On failure the
    last good value is kept and the retry is pushed back further each time.
    Returns (value, age_in_seconds) or (None, None)."""
    c = state["cache"].get(key) or {}
    t = now()
    have = c.get("v") is not None
    age = t - c.get("t", 0)
    if have and age < ttl:
        H.set(name, group, True, age=age)
        return c["v"], age
    if t < c.get("retry", 0):
        H.set(name, group, False, c.get("err", "unavailable"), age if have else 0)
        return (c["v"], age) if have else (None, None)
    try:
        v = fn()
        if v is None:
            raise ValueError("empty")
        state["cache"][key] = {"t": t, "v": v}
        H.set(name, group, True)
        return v, 0
    except Exception as e:
        c["fail"] = c.get("fail", 0) + 1
        c["retry"] = t + min(6 * 3600, 300 * 2 ** min(c["fail"], 8))
        c["err"] = err_text(e)
        state["cache"][key] = c
        H.set(name, group, False, c["err"], age if have else 0)
        return (c["v"], age) if have else (None, None)


def parallel(tasks, workers=10):
    """tasks: {key: callable}. Returns {key: result}."""
    out = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {k: ex.submit(fn) for k, fn in tasks.items()}
        for k, fu in futs.items():
            try:
                out[k] = fu.result()
            except Exception:
                out[k] = None
    return out


# --------------------------------------------------------------------------
# Which coins: top N by market value, skipping stablecoins and wrapped copies
# --------------------------------------------------------------------------
STABLES = {"USDT", "USDC", "DAI", "USDS", "USDE", "FDUSD", "PYUSD", "TUSD", "USD1", "BUSD",
           "USDD", "USDP", "GUSD", "FRAX", "LUSD", "SUSDE", "SUSDS", "USDY", "BUIDL", "RLUSD",
           "USDG", "USD0", "USDF", "USDTB", "EURC", "EURS", "XAUT", "PAXG", "BSC-USD", "USDX"}
WRAPPED = {"WBTC", "STETH", "WSTETH", "WETH", "WEETH", "CBBTC", "RETH", "WBETH", "LEO",
           "BTCB", "EZETH", "RSETH", "METH", "LBTC", "SOLVBTC", "JITOSOL", "MSOL", "BNSOL",
           "CBETH", "TBTC", "WBNB", "HBTC", "FBTC", "WTRX", "LSETH", "OSETH"}
NAME_SKIP = re.compile(r"usd|dollar|wrapped|staked|bridged|restaked|tether|liquid stak", re.I)
FALLBACK_UNIVERSE = [{"sym": "BTC", "name": "Bitcoin"}, {"sym": "ETH", "name": "Ethereum"},
                     {"sym": "BNB", "name": "BNB"}, {"sym": "XRP", "name": "XRP"},
                     {"sym": "SOL", "name": "Solana"}]


def _pick_universe(rows, n):
    out, seen = [], set()
    for sym, name, cap, price in sorted(rows, key=lambda r: -(r[2] or 0)):
        sym = (sym or "").upper()
        if not sym or sym in seen or sym in STABLES or sym in WRAPPED:
            continue
        if NAME_SKIP.search(name or "") or not re.fullmatch(r"[A-Z0-9]{2,6}", sym):
            continue
        if price and abs(price - 1) < 0.03 and "USD" in sym:
            continue
        seen.add(sym)
        out.append({"sym": sym, "name": name, "cap": cap})
        if len(out) >= n:
            break
    if len(out) < n:
        raise ValueError("too few coins")
    return out


def fetch_universe(n):
    errs = []
    try:
        j = http("https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd"
                 "&order=market_cap_desc&per_page=40&page=1")
        return _pick_universe([(r.get("symbol"), r.get("name"), f(r.get("market_cap"), 0),
                                f(r.get("current_price"))) for r in j], n)
    except Exception as e:
        errs.append(err_text(e))
    try:
        j = http("https://api.coinpaprika.com/v1/tickers?limit=40")
        return _pick_universe([(r.get("symbol"), r.get("name"),
                                f(r["quotes"]["USD"].get("market_cap"), 0),
                                f(r["quotes"]["USD"].get("price"))) for r in j], n)
    except Exception as e:
        errs.append(err_text(e))
    j = http("https://api.coinlore.net/api/tickers/?start=0&limit=40")
    return _pick_universe([(r.get("symbol"), r.get("name"), f(r.get("market_cap_usd"), 0),
                            f(r.get("price_usd"))) for r in j["data"]], n)


# --------------------------------------------------------------------------
# Prices, candles and order books. Four exchanges, first one that answers.
# Candle rows are [open_time, open, high, low, close, volume], oldest first.
# --------------------------------------------------------------------------
def _book(bids, asks):
    b = sorted(([f(x[0]), f(x[1])] for x in bids), key=lambda x: -x[0])
    a = sorted(([f(x[0]), f(x[1])] for x in asks), key=lambda x: x[0])
    b = [x for x in b if x[0] and x[1]]
    a = [x for x in a if x[0] and x[1]]
    if not b or not a or b[0][0] >= a[0][0] * 1.01:
        raise ValueError("bad book")
    mid = (b[0][0] + a[0][0]) / 2
    return {"bids": [x for x in b if x[0] > mid * 0.97][:400],
            "asks": [x for x in a if x[0] < mid * 1.03][:400]}


class Kraken:
    name = "Kraken"
    alias = {"BTC": "XBT", "DOGE": "XDG"}

    def _get(self, path):
        j = http("https://api.kraken.com/0/public/" + path)
        if j.get("error"):
            raise ValueError(str(j["error"])[:60])
        return next(v for k, v in j["result"].items() if k != "last")

    def candles(self, sym, minutes):
        rows = self._get("OHLC?pair=%sUSD&interval=%d" % (self.alias.get(sym, sym), minutes))
        return [[int(r[0]), f(r[1]), f(r[2]), f(r[3]), f(r[4]), f(r[6], 0)] for r in rows]

    def book(self, sym):
        d = self._get("Depth?pair=%sUSD&count=200" % self.alias.get(sym, sym))
        return _book(d["bids"], d["asks"])


class Coinbase:
    name = "Coinbase"

    def candles(self, sym, minutes):
        rows = http("https://api.exchange.coinbase.com/products/%s-USD/candles?granularity=%d"
                    % (sym, minutes * 60))
        return sorted([[int(r[0]), f(r[3]), f(r[2]), f(r[1]), f(r[4]), f(r[5], 0)] for r in rows])

    def book(self, sym):
        d = http("https://api.exchange.coinbase.com/products/%s-USD/book?level=2" % sym)
        return _book(d["bids"][:600], d["asks"][:600])


class Binance:
    name = "Binance"

    def candles(self, sym, minutes):
        iv = "5m" if minutes == 5 else "1h"
        rows = http("https://data-api.binance.vision/api/v3/klines?symbol=%sUSDT&interval=%s"
                    "&limit=500" % (sym, iv))
        return [[int(r[0]) // 1000, f(r[1]), f(r[2]), f(r[3]), f(r[4]), f(r[5], 0)] for r in rows]

    def book(self, sym):
        d = http("https://data-api.binance.vision/api/v3/depth?symbol=%sUSDT&limit=1000" % sym)
        return _book(d["bids"], d["asks"])


class OKX:
    name = "OKX"

    def candles(self, sym, minutes):
        bar = "5m" if minutes == 5 else "1H"
        j = http("https://www.okx.com/api/v5/market/candles?instId=%s-USDT&bar=%s&limit=300"
                 % (sym, bar))
        return sorted([[int(r[0]) // 1000, f(r[1]), f(r[2]), f(r[3]), f(r[4]), f(r[5], 0)]
                       for r in j["data"]])

    def book(self, sym):
        d = http("https://www.okx.com/api/v5/market/books?instId=%s-USDT&sz=400" % sym)["data"][0]
        return _book(d["bids"], d["asks"])


VENUES = [Kraken(), Coinbase(), Binance(), OKX()]


def _clean(rows):
    rows = [r for r in rows if None not in r[:5] and r[4] > 0]
    if len(rows) < 60:
        raise ValueError("too few candles")
    return rows


def fetch_market(sym, preferred=None):
    """Candles (5-minute and hourly) plus the live order book for one coin."""
    order = sorted(VENUES, key=lambda v: v.name != preferred)
    last = None
    for v in order:
        try:
            c5 = _clean(v.candles(sym, 5))
            if now() - c5[-1][0] > 1800:
                raise ValueError("stale prices")
            c60 = _clean(v.candles(sym, 60))
            try:
                book = v.book(sym)
            except Exception:
                book = None
            return {"venue": v.name, "c5": c5[-400:], "c60": c60[-400:], "book": book}
        except Exception as e:
            last = e
    raise last or ValueError("no market data")


# --------------------------------------------------------------------------
# Futures positioning: funding rates and open interest
# --------------------------------------------------------------------------
def fetch_hyperliquid(symbols):
    j = http("https://api.hyperliquid.xyz/info", data={"type": "metaAndAssetCtxs"})
    names = [u["name"] for u in j[0]["universe"]]
    out = {}
    for s in symbols:
        if s in names:
            c = j[1][names.index(s)]
            fr, oi, px = f(c.get("funding")), f(c.get("openInterest")), f(c.get("markPx"))
            if fr is not None:
                out[s] = {"apr": fr * 24 * 365 * 100,
                          "oi": oi * px if oi and px else None, "prem": f(c.get("premium"))}
    if not out:
        raise ValueError("no coins matched")
    return out


def fetch_okx_funding(sym):
    d = http("https://www.okx.com/api/v5/public/funding-rate?instId=%s-USDT-SWAP" % sym)["data"][0]
    hours = (f(d.get("nextFundingTime"), 0) - f(d.get("fundingTime"), 0)) / 3.6e6
    if not 0.5 <= hours <= 24:
        hours = 8
    return {"apr": f(d["fundingRate"]) / hours * 24 * 365 * 100}


# --------------------------------------------------------------------------
# News and social headlines
# --------------------------------------------------------------------------
NEWS_FEEDS = [
    ("CoinDesk", "crypto", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "crypto", "https://cointelegraph.com/rss"),
    ("Decrypt", "crypto", "https://decrypt.co/feed"),
    ("The Block", "crypto", "https://www.theblock.co/rss.xml"),
    ("CryptoSlate", "crypto", "https://cryptoslate.com/feed/"),
    ("Bitcoin Magazine", "crypto", "https://bitcoinmagazine.com/feed"),
    ("Google News: crypto", "crypto", "https://news.google.com/rss/search?q=" + urllib.parse.quote(
        "(bitcoin OR ethereum OR crypto OR solana OR XRP OR BNB) when:2h") + "&hl=en-US&gl=US&ceid=US:en"),
    ("Google News: economy", "macro", "https://news.google.com/rss/search?q=" + urllib.parse.quote(
        '("Federal Reserve" OR inflation OR tariffs OR "Wall Street" OR Treasury) markets when:3h')
     + "&hl=en-US&gl=US&ceid=US:en"),
    ("CNBC Economy", "macro", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258"),
    ("Federal Reserve", "macro", "https://www.federalreserve.gov/feeds/press_all.xml"),
]
SOCIAL_FEEDS = [
    ("Reddit r/CryptoCurrency", "social", "https://www.reddit.com/r/CryptoCurrency/hot/.rss?limit=30"),
    ("Reddit r/Bitcoin", "social", "https://www.reddit.com/r/Bitcoin/hot/.rss?limit=20"),
]
MACRO_WORDS = re.compile(
    r"\b(fed|fomc|powell|rate cut|rate hike|rates?|inflation|cpi|ppi|jobs|payrolls|unemployment|"
    r"tariffs?|treasur(y|ies)|yields?|recession|gdp|stocks|wall street|nasdaq|s&p|dollar|shutdown|"
    r"debt ceiling|war|strike|sanctions?|oil|banks?|crisis|stimulus|liquidity|sec|etfs?|crypto|"
    r"bitcoin|stablecoins?|default|selloff|sell-off|rally)\b", re.I)
ALIASES = {"BTC": ["bitcoin", "btc"], "ETH": ["ethereum", "ether", "eth"], "XRP": ["xrp", "ripple"],
           "BNB": ["bnb", "binance"], "SOL": ["solana", "sol"], "DOGE": ["dogecoin", "doge"],
           "TRX": ["tron", "trx"], "ADA": ["cardano", "ada"], "HYPE": ["hyperliquid", "hype"]}


def _tag(el):
    return el.tag.rsplit("}", 1)[-1].lower()


def _when(s):
    if not s:
        return None
    s = s.strip()
    try:
        return parsedate_to_datetime(s).timestamp()
    except Exception:
        pass
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
    except Exception:
        return None


def parse_feed(text, source, kind):
    """Reads RSS or Atom. Returns [{id, t, title, src, kind, url}]."""
    root = ET.fromstring(text.lstrip("﻿ \n\r\t").encode("utf-8"))
    out = []
    for el in root.iter():
        if _tag(el) not in ("item", "entry"):
            continue
        title = link = when = None
        for ch in el:
            tg = _tag(ch)
            if tg == "title":
                title = "".join(ch.itertext())
            elif tg == "link":
                link = link or ch.get("href") or (ch.text or "").strip()
            elif tg in ("pubdate", "published", "updated", "date"):
                when = when or _when(ch.text)
        if not title:
            continue
        title = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", title))).strip()
        src = source
        if source.startswith("Google News") and " - " in title:
            title, pub = title.rsplit(" - ", 1)
            src = pub.strip()[:30]
        if len(title) < 15:
            continue
        if not (link or "").startswith("http"):
            link = ""
        out.append({"id": hashlib.md5(title.lower().encode()).hexdigest()[:12], "t": when,
                    "title": title[:220], "src": src, "kind": kind, "url": link[:300]})
    return out


def fetch_feed(source, kind, url):
    items = parse_feed(http(url, raw=True, timeout=10, limit=3_000_000), source, kind)
    if not items:
        raise ValueError("no items")
    return items


def words(title):
    return set(re.findall(r"[a-z0-9$%]{3,}", title.lower()))


def tag_coins(title, universe):
    low = title.lower()
    hit = []
    for c in universe:
        keys = set(ALIASES.get(c["sym"], [])) | {c["sym"].lower(), (c.get("name") or "").lower()}
        if any(k and re.search(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(k), low) for k in keys):
            hit.append(c["sym"])
    return hit


# --------------------------------------------------------------------------
# Crowd mood
# --------------------------------------------------------------------------
def fetch_fear_greed():
    d = http("https://api.alternative.me/fng/?limit=10")["data"]
    return [[int(x["timestamp"]), int(x["value"]), x.get("value_classification", "")] for x in d]


# --------------------------------------------------------------------------
# On-chain: what is happening on the blockchains themselves
# --------------------------------------------------------------------------
def fetch_coinmetrics(sym):
    base = ("https://community-api.coinmetrics.io/v4/timeseries/asset-metrics?assets=%s&metrics=%s"
            "&frequency=1d&page_size=40&paging_from=end")
    tries = ["AdrActCnt,TxCnt", "AdrActCnt"]
    if sym in ("BTC", "ETH"):
        tries.insert(0, "AdrActCnt,TxCnt,FlowInExUSD,FlowOutExUSD,CapMVRVCur")
    last = None
    for metrics in tries:
        try:
            rows = sorted(http(base % (sym.lower(), metrics))["data"], key=lambda r: r["time"])
            if not rows:
                raise ValueError("empty")
            col = lambda k: [f(r.get(k)) for r in rows if f(r.get(k)) is not None]
            return {"t": _when(rows[-1]["time"][:19] + "+00:00"), "adr": col("AdrActCnt"),
                    "tx": col("TxCnt"), "fin": col("FlowInExUSD"), "fout": col("FlowOutExUSD"),
                    "mvrv": (col("CapMVRVCur") or [None])[-1]}
        except Exception as e:
            last = e
    raise last


def fetch_stablecoins():
    j = http("https://stablecoins.llama.fi/stablecoincharts/all", timeout=20)
    rows = [[int(r["date"]), f(r["totalCirculatingUSD"]["peggedUSD"])] for r in j[-45:]]
    rows = [r for r in rows if r[1]]
    if len(rows) < 10:
        raise ValueError("too short")
    return rows


CHAINS = {"ETH": "Ethereum", "SOL": "Solana", "BNB": "BSC", "XRP": "XRPL", "TRX": "Tron",
          "ADA": "Cardano", "AVAX": "Avalanche", "SUI": "Sui", "HYPE": "Hyperliquid L1"}


def fetch_dex_volume(chain):
    j = http("https://api.llama.fi/overview/dexs/%s?excludeTotalDataChart=true"
             "&excludeTotalDataChartBreakdown=true" % urllib.parse.quote(chain), timeout=15)
    a, b = f(j.get("total7d")), f(j.get("total14dto7d"))
    if not a or not b:
        raise ValueError("no volume")
    return {"wow": (a / b - 1) * 100, "vol7d": a}


# --------------------------------------------------------------------------
# Wider economy: stocks, the dollar, interest rates, scheduled announcements
# --------------------------------------------------------------------------
YAHOO = {"nq": "NQ=F", "dxy": "DX-Y.NYB", "tnx": "^TNX", "vix": "^VIX"}
FRED = {"nq": "NASDAQCOM", "dxy": "DTWEXBGS", "tnx": "DGS10", "vix": "VIXCLS"}


def _yahoo(symbol):
    j = http("https://query1.finance.yahoo.com/v8/finance/chart/%s?interval=1h&range=1mo"
             % urllib.parse.quote(symbol))
    r = j["chart"]["result"][0]
    rows = [[int(t), f(c)] for t, c in zip(r["timestamp"], r["indicators"]["quote"][0]["close"])]
    rows = [x for x in rows if x[1]]
    if len(rows) < 20:
        raise ValueError("too short")
    return rows[-200:]


def _fred(series):
    start = datetime.fromtimestamp(now() - 50 * 86400, timezone.utc).strftime("%Y-%m-%d")
    text = http("https://fred.stlouisfed.org/graph/fredgraph.csv?id=%s&cosd=%s" % (series, start),
                raw=True)
    rows = []
    for line in text.strip().splitlines()[1:]:
        d, _, v = line.partition(",")
        if f(v) is not None:
            rows.append([int(datetime.strptime(d.strip(), "%Y-%m-%d")
                             .replace(tzinfo=timezone.utc).timestamp()) + 21 * 3600, f(v)])
    if len(rows) < 8:
        raise ValueError("too short")
    return rows


def fetch_macro():
    got = parallel({k: (lambda s=s: _yahoo(s)) for k, s in YAHOO.items()}, 4)
    src = "live"
    missing = [k for k in YAHOO if not got.get(k)]
    if missing:
        more = parallel({k: (lambda s=FRED[k]: _fred(s)) for k in missing}, 4)
        got.update({k: v for k, v in more.items() if v})
        src = "daily" if len(missing) == len(YAHOO) else "mixed"
    got = {k: v for k, v in got.items() if v}
    if not got:
        raise ValueError("no macro data")
    got["src"] = src
    return got


FOMC_FALLBACK = ["2026-10-28T18:00:00+00:00", "2026-12-09T19:00:00+00:00"]


def fetch_calendar():
    try:
        j = http("https://nfs.faireconomy.media/ff_calendar_thisweek.json")
        ev = [{"t": _when(e["date"]), "title": e["title"][:60]} for e in j
              if e.get("country") == "USD" and e.get("impact") == "High" and _when(e.get("date"))]
        return {"events": ev, "src": "calendar"}
    except Exception:
        return {"events": [{"t": _when(d), "title": "Federal Reserve rate decision"}
                           for d in FOMC_FALLBACK], "src": "fallback"}


# --------------------------------------------------------------------------
# Language model for reading headlines (optional, free key)
# --------------------------------------------------------------------------
PROVIDERS = [
    ("GROQ_API_KEY", "Groq", "https://api.groq.com/openai/v1",
     ["gpt-oss-120b", "llama.*70b", "qwen", "gpt-oss-20b", "llama"]),
    ("GEMINI_API_KEY", "Gemini", "https://generativelanguage.googleapis.com/v1beta/openai",
     ["flash-lite", "flash"]),
]
MODEL_SKIP = re.compile(r"guard|whisper|tts|embed|image|audio|vision|live|preview|exp|orpheus|"
                        r"safeguard|learnlm|gemma|aqa|robotics|computer", re.I)


def llm_provider():
    if os.environ.get("LLM_API_KEY") and os.environ.get("LLM_BASE_URL"):
        return {"key": os.environ["LLM_API_KEY"], "name": "Custom",
                "base": os.environ["LLM_BASE_URL"].rstrip("/"),
                "prefs": [re.escape(os.environ.get("LLM_MODEL", ""))], "fixed": os.environ.get("LLM_MODEL")}
    for env, name, base, prefs in PROVIDERS:
        if os.environ.get(env, "").strip():
            return {"key": os.environ[env].strip(), "name": name, "base": base, "prefs": prefs}
    return None


def _pick_model(p):
    if p.get("fixed"):
        return p["fixed"]
    ids = [m["id"] for m in http(p["base"] + "/models",
                                 headers={"Authorization": "Bearer " + p["key"]})["data"]]
    ids = [i for i in ids if not MODEL_SKIP.search(i)]
    for pat in p["prefs"]:
        hit = sorted((i for i in ids if re.search(pat, i, re.I)), reverse=True)
        if hit:
            return hit[0]
    if ids:
        return ids[0]
    raise ValueError("no usable model")


def llm_json(state, system, user, max_tokens=2600):
    """Asks the model for a JSON answer. Returns (dict, tokens_used). Raises on failure."""
    p = llm_provider()
    if not p:
        raise RuntimeError("no key")
    L = state["llm"]
    if L.get("provider") != p["name"] or not L.get("model"):
        L["provider"], L["model"] = p["name"], _pick_model(p)
    body = {"model": L["model"], "temperature": 0.1, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"}}
    if "gpt-oss" in L["model"]:
        body["reasoning_effort"] = "low"       # keeps the answer short and inside the free limits
    hdr = {"Authorization": "Bearer " + p["key"]}
    try:
        j = http(p["base"] + "/chat/completions", data=body, headers=hdr, timeout=45)
    except urllib.error.HTTPError as e:
        if e.code in (400, 404, 422):  # model renamed, or option not supported: try once more
            L["model"] = _pick_model(p)
            body["model"] = L["model"]
            body.pop("response_format", None)
            body.pop("reasoning_effort", None)
            j = http(p["base"] + "/chat/completions", data=body, headers=hdr, timeout=45)
        else:
            raise
    text = j["choices"][0]["message"]["content"] or ""
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        raise ValueError("no JSON in answer")
    used = int((j.get("usage") or {}).get("total_tokens") or (len(system) + len(user) + len(text)) / 3.5)
    return json.loads(text[a:b + 1]), used
