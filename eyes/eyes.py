"""عيون المكتب (eyes-1.0, 7 Nov 2026): a plain calculator that watches the market during the session and rings the owner's
phone when a MEASURED setup appears. No AI, no portfolio, paper only. Standard library only (runs on GitHub Actions).

Modes:
  intraday   one check: market shock, big movers on the watch list (with the headlines that explain them),
             on a shock day a sweep of the S&P 500 for panic-rebound candidates (buy near the close), sell reminder next day.
  session    eyes-2.0 (11 Nov 2026): GitHub's own schedule proved unreliable (2 of about 80 planned checks ran in two days), so a
             chain of jobs stays awake from Monday morning to Friday evening: in the session one check every 5 minutes, after the
             close the nightly work, and every 5.5 hours the job starts the next one itself and stops. Needs free minutes
             (a public repository); a private repository keeps the single checks.
  nightly    after the close: daily bars of the whole market (Massive grouped, 1 call), quiet deep drop candidates for the
             next open, fear spike (VIX first close >= 30 in 20 sessions), a day file for the desk.
  test       sends one test ring so the owner knows the phone is set up.
  selftest   offline checks of the rules (no network, no keys).

Keys come from the environment (GitHub secrets): FINNHUB_API_KEY, MASSIVE_API_KEY, NTFY_TOPIC. They are never printed.
Measured numbers come from eyes/measured.json (built from the desk brain, cpu brain.panic_rebound + brain.playbook).
"""
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    from zoneinfo import ZoneInfo
    NY = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    NY = None

VERSION = "eyes-2.1"
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.environ.get("EYES_STATE", os.path.join(HERE, "state"))
OUT_DIR = os.path.join(HERE, "out")
FINNHUB = "https://finnhub.io/api/v1"
MASSIVE = "https://api.massive.com"
NTFY = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
UA = "cio-desk-eyes/1.0"

# live rules (from the desk brain; the measured stats are read from measured.json)
SHOCK = {"SPY": -2.5, "QQQ": -3.0, "SOXX": -5.0}          # shock_live_en
EARLY = {"SPY": -1.8, "QQQ": -2.2, "SOXX": -3.5}          # early warning (no idea, just eyes open)
PANIC_DROP = -10.0                                         # candidate: down >= 10% on the day
WAVE_MULT = 1.5                                            # fell no more than 1.5 x beta x SPY
CHIPS_MULT = 1.6                                           # or on a chips day no more than 1.6 x SOXX
DEFAULT_BETA = 1.5
SWEEP_FROM = (14, 30)                                      # NY time: sweep the S&P 500 on a shock day from 2:30 PM
SELL_REMIND_AT = (15, 30)                                  # NY time: next-day sell reminder
MOVER_PCT = 8.0                                            # watch list: ring (quietly) on a +-8% move with the headlines
QUIET = {"A": 15.0, "B": 12.0}                             # quiet deep drop tiers (5-session fall, no big-volume day)
OWN_NEWS_RX = re.compile(r"(?i)\b(earnings|results|quarter|guidance|outlook|forecast|downgrad|cut to|price target|offering|"
                         r"lawsuit|probe|investigat|sec |fda|trial|recall|ceo|resign|merger|acqui|deal|buyout|bankrupt|"
                         r"delist|restat|miss(es|ed)?|beat|warn)")

PACE = 1.05   # Finnhub free plan: 60 calls a minute

# eyes-2.1 (owner note 67, 12 Nov 2026: «عاجل بتوصلني بوقتها، مش بعد ساعتين»; NVDA's chief executive resigned at 8:48 AM New York
# time and the owner heard from the desk 1.5 hours later, after the stock had touched 148). The owner's HOLDINGS (secret HOLDINGS,
# else holdings.txt) are watched every 5 minutes from 7:00 AM to 8:00 PM New York time: a critical headline rings at once (loud),
# and in the session every new step of the day's move rings (5, 8, 12, 15, 20%).
CRIT_RX = re.compile(r"(?i)\b(ceo|chief executive|resign|steps? down|stepping down|ousted|oust|fired|terminat|succession|interim|"
                     r"investigat|probe|fbi|doj|subpoena|sec charges|indict|fraud|guidance cut|cuts? (its )?(guidance|outlook|forecast)|"
                     r"lowers? (guidance|outlook|forecast)|withdraws? guidance|offering|dilut|convertible|bankrupt|chapter 11|delist|halt|"
                     r"recall|downgrade|short seller|short report|restat|export (ban|curb|restriction)|sanction)")
MOVE_STEPS = (5.0, 8.0, 12.0, 15.0, 20.0)
EXT_FROM, EXT_TO = (7, 0), (20, 0)   # New York time: holdings news outside the session too


# ----------------------------------------------------------------------------------------------------------------- utils
def now_ny():
    return dt.datetime.now(NY) if NY else dt.datetime.utcnow() - dt.timedelta(hours=5)


def t12(d):
    """12-hour Levantine time: 2:45 بعد الضهر"""
    h, m = d.hour, d.minute
    part = "الصبح" if h < 12 else ("الضهر" if h < 14 else ("بعد الضهر" if h < 18 else "المسا"))
    h12 = h % 12 or 12
    return "%d:%02d %s" % (h12, m, part)


def secret(name):
    v = os.environ.get(name, "").strip()
    return v or None


def clean_err(e):
    """an error text with any token removed"""
    s = str(e)
    for k in ("FINNHUB_API_KEY", "MASSIVE_API_KEY", "NTFY_TOPIC"):
        v = secret(k)
        if v:
            s = s.replace(v, "***")
    return re.sub(r"(token|apiKey)=[^&\s]+", r"\1=***", s)


def http_get(url, timeout=20, raw=False):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        b = r.read()
    return b.decode("utf-8", "replace") if raw else json.loads(b.decode("utf-8"))


def load(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)
    os.replace(tmp, path)


def lines(name):
    try:
        with open(os.path.join(HERE, name), encoding="utf-8") as fh:
            return [x.strip().upper() for x in fh if x.strip() and not x.startswith("#")]
    except Exception:
        return []


MEASURED = load(os.path.join(HERE, "measured.json"), {})
BETAS = load(os.path.join(HERE, "betas.json"), {})


PUBLIC_LOG = os.environ.get("EYES_PUBLIC") == "1"


# ----------------------------------------------------------------------------------------------------------------- ring
def ring(title, message, priority=3, tags=None, dry=False):
    """send one notification to the owner's phone (ntfy). priority 5 = urgent (loud, through do-not-disturb)."""
    topic = secret("NTFY_TOPIC")
    body = {"topic": topic or "", "title": title, "message": message, "priority": int(priority), "tags": tags or []}
    if PUBLIC_LOG:   # a public repository shows its run log to everyone: no names, no text
        print("RING p%d (text kept out of the public log)" % priority)
    else:
        print("RING p%d | %s | %s" % (priority, title, message.replace("\n", " / ")[:300]))
    if dry or not topic:
        if not topic:
            print("  (no NTFY_TOPIC: not sent)")
        return False
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(NTFY.rstrip("/") + "/", data=data, headers={"User-Agent": UA, "Content-Type": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return 200 <= r.status < 300
        except Exception as e:
            print("  ring failed:", clean_err(e))
            time.sleep(3)
    return False


# ----------------------------------------------------------------------------------------------------------------- data
class Finnhub:
    def __init__(self, key):
        self.key = key
        self.calls = 0
        self.last = 0.0

    def get(self, path, **q):
        wait = PACE - (time.time() - self.last)
        if wait > 0:
            time.sleep(wait)
        q["token"] = self.key
        url = FINNHUB + path + "?" + urllib.parse.urlencode(q)
        self.last = time.time()
        self.calls += 1
        try:
            return http_get(url)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(20)
                self.last = time.time()
                try:
                    return http_get(url)
                except Exception as e2:
                    print("finnhub", path, "" if PUBLIC_LOG else q.get("symbol"), clean_err(e2))
                    return None
            print("finnhub", path, "" if PUBLIC_LOG else q.get("symbol"), clean_err(e))
            return None
        except Exception as e:
            print("finnhub", path, "" if PUBLIC_LOG else q.get("symbol"), clean_err(e))
            return None

    def quote(self, sym):
        r = self.get("/quote", symbol=sym)
        if not r or not r.get("c") or not r.get("pc"):
            return None
        return {"sym": sym, "last": r["c"], "prev": r["pc"], "pct": (r["c"] / r["pc"] - 1) * 100, "high": r.get("h"),
                "low": r.get("l"), "t": r.get("t")}

    def news(self, sym, days=2):
        to = now_ny().date()
        fr = to - dt.timedelta(days=days + 2)
        r = self.get("/company-news", symbol=sym, **{"from": fr.isoformat(), "to": to.isoformat()})
        if not isinstance(r, list):
            return []
        r.sort(key=lambda x: -(x.get("datetime") or 0))
        cut = time.time() - (days + 1) * 86400
        return [{"h": x.get("headline", "")[:160], "src": x.get("source", ""), "at": x.get("datetime")} for x in r
                if (x.get("datetime") or 0) >= cut][:6]


def vix_live():
    """VIX now (Cboe delayed quote, free) or None"""
    try:
        r = http_get("https://cdn.cboe.com/api/global/delayed_quotes/quotes/_VIX.json")
        d = r.get("data") or {}
        return {"last": float(d["current_price"]), "prev": float(d["prev_day_close"])}
    except Exception as e:
        print("vix live:", clean_err(e))
        return None


def vix_history():
    """[(date, close)] from Cboe, else FRED VIXCLS (both free, no key)"""
    rows = []
    try:
        txt = http_get("https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv", raw=True)
        for ln in txt.splitlines()[1:]:
            p = ln.split(",")
            if len(p) >= 5:
                m, d_, y = p[0].split("/")
                rows.append(("%s-%s-%s" % (y, m.zfill(2), d_.zfill(2)), float(p[4])))
    except Exception as e:
        print("vix cboe:", clean_err(e))
    if not rows:
        try:
            txt = http_get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=VIXCLS", raw=True)
            for ln in txt.splitlines()[1:]:
                p = ln.split(",")
                if len(p) == 2 and p[1] not in (".", ""):
                    rows.append((p[0], float(p[1])))
        except Exception as e:
            print("vix fred:", clean_err(e))
    return rows[-400:]


# ----------------------------------------------------------------------------------------------------------------- rules
def is_wave(pct, sym, spy_pct, soxx_pct):
    """the stock fell WITH the market (not on its own story): no more than 1.5 x beta x SPY's fall, or on a chips day
    (SOXX <= -5%) a chips name no more than 1.6 x SOXX's fall"""
    beta = (BETAS.get("beta") or {}).get(sym, DEFAULT_BETA)
    by_beta = spy_pct < 0 and beta > 0 and pct >= WAVE_MULT * beta * spy_pct
    chips = sym in (BETAS.get("chips") or []) and soxx_pct is not None and soxx_pct <= SHOCK["SOXX"] and pct >= CHIPS_MULT * soxx_pct
    return by_beta or chips, beta


def panic_grade(vix, wave_by_beta=True):
    gs = {g["id"]: g for g in (MEASURED.get("panic_rebound") or {}).get("grades", [])}
    if vix is not None and vix >= 30 and wave_by_beta:
        return gs.get("A") or {"id": "A", "win": 0.746, "avg": 4.62, "p10": -7.17}
    return gs.get("B") or {"id": "B", "win": 0.691, "avg": 3.54, "p10": -7.42}


def own_news(heads):
    """headlines that say the stock has its own story (then it is not a wave idea)"""
    return [h for h in heads if OWN_NEWS_RX.search(h.get("h", ""))]


def quiet_scan(hist, members, spy="SPY"):
    """quiet deep drop on the last day of hist: fell >= 12% in 5 sessions, no day in the last 5 with volume >= 2x the
    50-day average, close above its 200-day average. hist = {"dates": [...], "c": {t: [...]}, "v": {t: [...]}}"""
    out = []
    ds = hist.get("dates") or []
    if len(ds) < 206:
        return out
    C, V = hist.get("c") or {}, hist.get("v") or {}
    for t in members:
        c, v = C.get(t), V.get(t)
        if not c or not v or len(c) != len(ds) or c[-1] is None or c[-6] is None:
            continue
        last = c[-1]
        if last < 5:
            continue
        r5 = (last / c[-6] - 1) * 100
        if r5 > -QUIET["B"]:
            continue
        w200 = [x for x in c[-200:] if x]
        if len(w200) < 190 or last <= sum(w200) / len(w200):
            continue
        v50 = [x for x in v[-56:-5] if x]
        vmax5 = max([x for x in v[-5:] if x] or [0])
        if len(v50) < 40 or vmax5 >= 2 * (sum(v50) / len(v50)):
            continue
        # a split or a bad print makes a fake drop: skip any day-to-day jump bigger than 40%
        seg = [x for x in c[-7:] if x]
        if any(abs(seg[i] / seg[i - 1] - 1) > 0.4 for i in range(1, len(seg))):
            continue
        tier = "A" if r5 <= -QUIET["A"] else "B"
        low5 = min(x for x in c[-5:] if x)
        out.append({"t": t, "r5": round(r5, 1), "last": last, "tier": tier, "low5": low5})
    out.sort(key=lambda x: x["r5"])
    return out


def fear_spike(rows):
    """VIX closed >= 30 today for the first time in 20 sessions"""
    if len(rows) < 22:
        return None
    today = rows[-1][1]
    prior = [x[1] for x in rows[-21:-1]]
    if today >= 30 and max(prior) < 30:
        return {"date": rows[-1][0], "vix": today, "level": 40 if today >= 40 else 30}
    return None


def stats_line(win, avg, p10=None):
    s = "من كل 100 مرة ربحت %d، ومعدّل الربح %+.1f%%" % (round(win * 100), avg)
    if p10 is not None:
        s += "، وبأسوأ 10 مرات من كل 100: %+.1f%% أو أسوأ" % p10
    return s


# ----------------------------------------------------------------------------------------------------------------- intraday
def plan_line(q, heads, held, spy_pct=None, soxx_pct=None):
    """owner note 68 (12 Nov 2026: «ما عطاني دخول وخروج»): every move alert ends with an entry line and an exit line. A plan with
    numbers is given only where a measured edge exists (a big fall with the market wave); elsewhere the honest line is «no entry»
    with the exit rule. Numbers: S&P 500 members 2000-2026 (desk lab: panic_rebound, situation_rates)."""
    pct, last = q["pct"], q["last"]
    own = bool(own_news(heads)) or any(CRIT_RX.search(h.get("h", "")) for h in heads)
    if pct < 0:
        wave = (not own) and spy_pct is not None and is_wave(pct, q["sym"], spy_pct, soxx_pct if soxx_pct is not None else 0)[0]
        low = q.get("low")
        if own:
            # owner note 69, lab/own_drop_stop.py (3,571 own-news falls of 10%+ on S&P 500 members, 2000-2026): a stop under the day's
            # low is hit 69 times in 100 within 20 sessions and turns the average from +0.5% to -0.2% (60 sessions: +2.7% to -0.4%);
            # it halves the worst 1 in 10 (-18% to -8%). The size cap protects as well without the cost, so the rule is the size.
            return ("دخول: لا. الوقعة على خبر الشركة نفسها، وبالتاريخ بيطلع تاني يوم 47 مرة من 100 بس (معدّل ‎-0.4%‎).\n"
                    "خروج: " + ("إذا السهم فوق حدّه بمحفظتك، خفّف للحد اليوم، وخلّي الباقي بلا وقف قريب: الوقف تحت أوطى اليوم بينضرب 69 مرة من 100 خلال شهر، وبيقلب المعدّل لخسارة." if held else "ما في صفقة."))
        if wave and pct <= PANIC_DROP:
            g = panic_grade(None)
            if held:
                return ("دخول: لا تزيد، عندك منه (التعزيز على النازل ممنوع).\n"
                        "خروج: خلّي. الوقعة مع موجة السوق بلا خبر خاص، وبالتاريخ بيطلع تاني يوم %d مرة من 100 (معدّل +%.1f%%)." % (round(g["win"] * 100), g["avg"]))
            return ("دخول: قرب السكرة (3:50 العصر بنيويورك) حوالي %.2f، حجم صغير.\n"
                    "خروج: سكرة بكرا مهما صار. بالتاريخ طلع %d مرة من 100، معدّل +%.1f%%. ما في وقف قريب لأنو بينضرب أغلب الأوقات، والحماية من الحجم." % (last, round(g["win"] * 100), g["avg"]))
        return ("دخول: لا. نزلة مع السوق بلا أفضلية مقاسة.\n"
                "خروج: " + ("خلّي، إلا إذا السهم فوق حدّه بمحفظتك." if held else "ما في صفقة."))
    return ("دخول: لا تلحق. الطلعة الكبيرة بيوم ما إلها أفضلية بعدها (بيسبق السوق 53 مرة من 100 على 40 جلسة).\n"
            "خروج: " + ("إذا صار فوق حدّه بمحفظتك، خفّف للحد وخود ربح الزيادة؛ غير هيك خلّي." if held else "ما في صفقة."))


def holdings_list():
    return [x.strip().upper() for x in re.split(r"[,\s]+", os.environ.get("HOLDINGS", "")) if x.strip()] or lines("holdings.txt")


def holdings_watch(fh, st, n, once, in_session=True, quotes=None):
    """the owner's holdings: a new critical headline rings at once (priority 5); in the session each new step of the day's move rings."""
    hold = [t for t in holdings_list() if re.fullmatch(r"[A-Z][A-Z.\-]{0,5}", t)][:25]
    seen = st.setdefault("seen_heads", [])
    quotes = quotes if quotes is not None else {}
    mk = {}
    if in_session and hold:
        for s_ in ("SPY", "SOXX"):
            qq = fh.quote(s_)
            mk[s_] = qq["pct"] if qq else None
    newsc, stepped = {}, set()
    for t in hold:
        q = quotes.get(t) or (fh.quote(t) if in_session else None)
        if q:
            quotes[t] = q
            step = max([x for x in MOVE_STEPS if abs(q["pct"]) >= x] or [0])
            if step:
                newsc[t] = fh.news(t, 1)
                if ("hstep:%s:%s:%d" % (t, "d" if q["pct"] < 0 else "u", step)) not in st.get("sent", {}):
                    stepped.add(t)
                word = "نازل" if q["pct"] < 0 else "طالع"
                low = q.get("low") if q["pct"] < 0 else q.get("high")
                once("hstep:%s:%s:%d" % (t, "d" if q["pct"] < 0 else "u", step),
                     "%s %s %+.1f%% (%s)" % (t, word, q["pct"], t12(n)),
                     "سهم بمحفظتك.\nالسعر %.2f%s.\nعدّى %d%% اليوم.%s\n%s" % (q["last"], (" · أوطى اليوم %.2f" % low) if (low and q["pct"] < 0) else ((" · أعلى اليوم %.2f" % low) if low else ""), int(step),
                      ("\nليش: " + newsc[t][0]["h"]) if newsc.get(t) else "", plan_line(q, newsc.get(t) or [], True, mk.get("SPY"), mk.get("SOXX"))),
                     priority=5 if step >= 8 else 4, tags=["rotating_light" if q["pct"] < 0 else "chart_with_upwards_trend"])
        heads = newsc.get(t) if t in newsc else fh.news(t, 1)
        for h in heads[:6]:
            k = "%s|%s" % (t, re.sub(r"\W+", " ", h.get("h", "").lower()).strip()[:90])
            if k in seen:
                continue
            seen.append(k)
            if t in stepped and newsc.get(t) and h is newsc[t][0]:
                continue   # the step alert of this check already carries this headline: one ring, not two
            if CRIT_RX.search(h.get("h", "")) and (time.time() - (h.get("at") or 0)) < 6 * 3600:
                px = ("\nالسعر %.2f (%+.1f%%)" % (q["last"], q["pct"])) if q else ""
                pl = plan_line(q, [h], True, mk.get("SPY"), mk.get("SOXX")) if q else ("دخول: لا قبل ما يفتح السوق ويبيّن ردّ الفعل.\n"
                     "خروج: إذا السهم فوق حدّه بمحفظتك، خفّف للحد أول الجلسة.")
                once("hnews:" + k, "عاجل: خبر على %s (%s)" % (t, t12(n)),
                     "سهم بمحفظتك." + px + "\nالخبر:\n" + h.get("h", "") + ("\n" + h.get("src", "") if h.get("src") else "") + "\n" + pl,
                     priority=5, tags=["rotating_light"])
    del seen[:-600]
    return quotes


def extended(dry=False):
    """outside the session (7:00 AM to 9:33 AM and 4:05 PM to 8:00 PM New York time): the holdings' critical news only."""
    key = secret("FINNHUB_API_KEY")
    if not key:
        return 1
    fh, n = Finnhub(key), now_ny()
    stp = os.path.join(STATE_DIR, "state.json")
    st = load(stp, {})
    today = n.date().isoformat()
    if st.get("day") != today:
        prev = {"day": st.get("day"), "panic_ideas": st.get("panic_ideas") or []}
        st = {"day": today, "sent": {}, "prev": prev if prev["panic_ideas"] else st.get("prev"), "log": [], "seen_heads": st.get("seen_heads", [])}

    def once(k, *a, **kw):
        if k in st["sent"]:
            return False
        ok = ring(*a, dry=dry, **kw)
        st["sent"][k] = n.isoformat()
        st["log"].append({"k": k, "at": n.isoformat(), "title": a[0], "msg": a[1], "p": kw.get("priority", 3)})
        return ok
    holdings_watch(fh, st, n, once, in_session=False)
    save(stp, st)
    return 0


def intraday(dry=False):
    key = secret("FINNHUB_API_KEY")
    if not key:
        print("no FINNHUB_API_KEY: stop")
        return 1
    fh = Finnhub(key)
    n = now_ny()
    today = n.date().isoformat()
    if n.weekday() >= 5:
        print("weekend: nothing")
        return 0
    if (n.hour, n.minute) < (9, 33) or (n.hour, n.minute) > (16, 5):
        print("outside the session (%s): nothing" % t12(n))
        return 0
    stp = os.path.join(STATE_DIR, "state.json")
    st = load(stp, {})
    if st.get("day") != today:
        prev = {"day": st.get("day"), "panic_ideas": st.get("panic_ideas") or []}
        st = {"day": today, "sent": {}, "prev": prev if prev["panic_ideas"] else st.get("prev"), "log": [], "seen_heads": st.get("seen_heads", [])}

    def once(k, *a, **kw):
        if k in st["sent"]:
            return False
        ok = ring(*a, dry=dry, **kw)
        st["sent"][k] = n.isoformat()
        st["log"].append({"k": k, "at": n.isoformat(), "title": a[0], "msg": a[1], "p": kw.get("priority", 3)})
        return ok

    # 0) eyes-2.1: the owner's holdings first (critical news at once, each new step of the move)
    hq = {}
    try:
        hq = holdings_watch(fh, st, n, once, in_session=True)
    except Exception as e:
        print("holdings check failed:", clean_err(e))
    save(stp, st)

    # 1) the market
    idx = {s: fh.quote(s) for s in ("SPY", "QQQ", "SOXX")}
    spy = idx.get("SPY")
    if not spy:
        print("no SPY quote: stop")
        save(stp, st)
        return 1
    # market closed today (holiday): the quote time is not today
    if spy.get("t") and dt.datetime.fromtimestamp(spy["t"], NY).date().isoformat() != today:
        print("SPY quote is not from today: market closed")
        save(stp, st)
        return 0
    vx = vix_live()
    pct = {s: (q["pct"] if q else None) for s, q in idx.items()}
    st["market"] = {"at": n.isoformat(), "pct": {k: (round(v, 2) if v is not None else None) for k, v in pct.items()},
                    "vix": vx["last"] if vx else None}
    mk = "SPY %+.1f%%، QQQ %+.1f%%، الرقائق %+.1f%%" % (pct["SPY"], pct["QQQ"] or 0, pct["SOXX"] or 0)
    if vx:
        mk += "، الخوف (VIX) %.1f" % vx["last"]
    shock = any(pct.get(s) is not None and pct[s] <= SHOCK[s] for s in SHOCK)
    early = any(pct.get(s) is not None and pct[s] <= EARLY[s] for s in EARLY)
    if shock:
        st["shock"] = True
        g = panic_grade(vx["last"] if vx else None)
        once("shock", "ذعر بالسوق هلّق (%s)" % t12(n),
             mk + "\nهيدا يوم ذعر حسب القاعدة. ما تبيع بالذعر. من الساعة 2:30 بعد الضهر بفتّش أسهم S&P 500 يلي نزلت 10% أو أكتر مع الموجة، "
             "وببعتلك الأفكار لتشتري قبل السكرة وتبيع بكرا عالسكرة.\nالمقاس من 2000: " + stats_line(g["win"], g["avg"], g.get("p10")) + ".",
             priority=5, tags=["rotating_light"])
    elif early:
        once("early", "السوق عم ينزل بقوة (%s)" % t12(n), mk + "\nلسا مش ذعر. من 2000، 8 من كل 10 مرات متل هيك السوق بيرجع وبيسكّر أحسن من -2%. إذا كمّل برنّ عليك.", priority=3,
             tags=["warning"])
    if vx and vx["last"] >= 30 and vx.get("prev", 0) < 30:
        fs = MEASURED.get("fear_spike_index") or {}
        lv = (fs.get("by_level") or {}).get("vix40" if vx["last"] >= 40 else "vix30") or {}
        once("vix30", "الخوف فوق 30 (%s)" % t12(n),
             "VIX هلّق %.1f. إذا سكّر فوق 30 وكانت أول مرة بـ20 جلسة، هيدي إشارة الخوف الكبير على المؤشر: QQQ بعد 60 جلسة %+.1f%% (ربح %d من 100). "
             "شرا بالتقسيط: نص عالسكرة ونص إذا نزل كمان 5%%." % (vx["last"], lv.get("qqq_60d", 4.56), round(lv.get("win", 0.69) * 100)),
             priority=4, tags=["chart_with_downwards_trend"])

    # 2) the watch list: big movers and why
    wl = [x.strip().upper() for x in re.split(r"[,\s]+", os.environ.get("WATCHLIST", "")) if x.strip()] or lines("watch.txt")
    watch = [t for t in wl if t not in idx and t not in hq][:30]
    quotes = {}
    for t in watch:
        q = fh.quote(t)
        if q:
            quotes[t] = q
    for t, q in sorted(quotes.items(), key=lambda kv: kv[1]["pct"]):
        if abs(q["pct"]) >= MOVER_PCT:
            heads = fh.news(t, 1)
            why = "\n".join("• " + h["h"] for h in heads[:2]) or "ما لقيت خبر للشركة: الحركة غالباً من السوق أو القطاع."
            if q["pct"] <= -20 and not shock:
                why += "\nوقعة كبيرة على خبر خاص: ما بتنلحق كصفقة سريعة. يلي بيحسمها نوع الشركة وسبب الوقعة: اطلب دراسة من المستشار."
            word = "نازل" if q["pct"] < 0 else "طالع"
            once("mover:" + t, "%s %s %+.1f%% (%s)" % (t, word, q["pct"], t12(n)), "السعر %.2f.\nليش:\n%s\n%s" % (q["last"], why,
                 plan_line(q, heads, False, (idx.get("SPY") or {}).get("pct"), (idx.get("SOXX") or {}).get("pct"))),
                 priority=3, tags=["mag"])

    # 3) shock day: sweep the S&P 500 for panic-rebound candidates, once, from 2:30 PM
    still = any(pct.get(s) is not None and pct[s] <= SHOCK[s] for s in SHOCK)   # the panic must still be on at sweep time
    if st.get("shock") and still and (n.hour, n.minute) >= SWEEP_FROM and not st.get("swept"):
        st["swept"] = n.isoformat()
        save(stp, st)
        ideas = sweep(fh, pct, vx, quotes)
        st["panic_ideas"] = ideas
        if ideas:
            g = panic_grade(vx["last"] if vx else None)
            body = []
            for i, x in enumerate(ideas[:5], 1):
                body.append("%d) %s %+.1f%% على %.2f، وقف %.2f" % (i, x["t"], x["pct"], x["last"], x["last"] * 0.92))
            once("panic_ideas", "أفكار يوم الذعر: اشتري قبل السكرة (%s)" % t12(n),
                 "\n".join(body) + "\nالقاعدة: شرا قبل سكرة اليوم، بيع بكرا عالسكرة. لا تمسكها يومين (المقاس بيصير أسوأ).\n"
                 "المقاس من 2000 (درجة %s): %s.\nسلة أعمق 5 أسهم: ربح 60 من 100 يوم، معدّل +2.5%%." % (g["id"], stats_line(g["win"], g["avg"], g.get("p10"))),
                 priority=5, tags=["moneybag"])
        else:
            once("panic_none", "يوم ذعر بلا فكرة نظيفة (%s)" % t12(n),
                 "فتّشت أسهم S&P 500: ما في سهم نزل 10% مع الموجة بلا خبر خاص فيه. ما في صفقة اليوم.", priority=3)

    # 4) the day after: sell reminder
    prev = st.get("prev") or {}
    if prev.get("panic_ideas") and (n.hour, n.minute) >= SELL_REMIND_AT:
        names = "، ".join(x["t"] for x in prev["panic_ideas"][:5])
        once("sell_reminder", "بيع أفكار امبارح عالسكرة (%s)" % t12(n),
             "%s: القاعدة بيع اليوم قبل السكرة. المسك ليوم تاني نتيجته أضعف (ربح 52 من 100 بدل 60)." % names, priority=5, tags=["bell"])

    if not st["sent"].get("hello"):
        st["sent"]["hello"] = n.isoformat()
        ring("العيون شغّالة (%s)" % t12(n), mk, priority=1, dry=dry)
    st["calls"] = fh.calls
    save(stp, st)
    write_day(st)
    print("calls", fh.calls)
    return 0


def sweep(fh, pct, vx, have):
    """S&P 500 members down >= 10% today that fell with the wave and have no story of their own"""
    spy_pct, soxx_pct = pct.get("SPY"), pct.get("SOXX")
    found = []
    for t in lines("sp500.txt"):
        q = have.get(t) or fh.quote(t)
        if not q or q["pct"] > PANIC_DROP:
            continue
        wave, beta = is_wave(q["pct"], t, spy_pct, soxx_pct)
        found.append({"t": t, "pct": round(q["pct"], 2), "last": q["last"], "beta": beta, "wave": wave})
    found.sort(key=lambda x: x["pct"])
    ideas = []
    for x in found:
        if not x["wave"]:
            continue
        heads = fh.news(x["t"], 1)
        own = own_news(heads)
        x["news"] = [h["h"] for h in heads[:3]]
        if own:
            x["own"] = own[0]["h"]
            continue
        ideas.append(x)
        if len(ideas) >= 5:
            break
    print("sweep: %d down >= 10%%, %d ideas" % (len(found), len(ideas)))
    return ideas


# ----------------------------------------------------------------------------------------------------------------- nightly
def grouped(key, day):
    url = MASSIVE + "/v2/aggs/grouped/locale/us/market/stocks/%s?adjusted=true&apiKey=%s" % (day, key)
    try:
        r = http_get(url, timeout=60)
    except Exception as e:
        print("grouped", day, clean_err(e))
        return None
    if r.get("resultsCount", 0) == 0:
        return {}
    return {x["T"]: (x.get("c"), x.get("v")) for x in r.get("results", [])}


def update_hist(key, keep):
    hp = os.path.join(STATE_DIR, "hist.json")
    h = load(hp, {"dates": [], "c": {}, "v": {}})
    have = set(h["dates"])
    n = now_ny()
    end = n.date() if (n.hour, n.minute) >= (16, 30) else n.date() - dt.timedelta(days=1)
    start = end - dt.timedelta(days=380) if len(h["dates"]) < 210 else end - dt.timedelta(days=10)
    days = []
    d = start
    while d <= end:
        if d.weekday() < 5 and d.isoformat() not in have:
            days.append(d.isoformat())
        d += dt.timedelta(days=1)
    print("history: %d days held, %d to fetch" % (len(h["dates"]), len(days)))
    new = {}
    for i, day in enumerate(days):
        g = grouped(key, day)
        if g is None:
            time.sleep(13)
            continue
        if g:
            new[day] = g
        if i < len(days) - 1:
            time.sleep(12.6)   # free plan: 5 calls a minute
    if new:
        dates = sorted(set(h["dates"]) | set(new))[-260:]
        C, V = {}, {}
        for t in keep:
            oc, ov = dict(zip(h["dates"], h["c"].get(t, []))), dict(zip(h["dates"], h["v"].get(t, [])))
            for day, g in new.items():
                if t in g:
                    oc[day], ov[day] = g[t]
            C[t] = [oc.get(x) for x in dates]
            V[t] = [ov.get(x) for x in dates]
        h = {"dates": dates, "c": C, "v": V}
        save(hp, h)
    return h


def nightly(dry=False):
    n = now_ny()
    today = n.date().isoformat()
    key = secret("MASSIVE_API_KEY")
    fkey = secret("FINNHUB_API_KEY")
    members = lines("sp500.txt")
    keep = sorted(set(members) | {"SPY", "QQQ", "SOXX"})
    night = {"day": today, "at": n.isoformat(), "version": VERSION, "quiet": [], "fear": None}
    if key:
        h = update_hist(key, keep)
        if h.get("dates") and h["dates"][-1] == today:
            fh = Finnhub(fkey) if fkey else None
            q = quiet_scan(h, members)
            tiers = {t["id"]: t for t in (MEASURED.get("quiet_drop_bounce") or {}).get("tiers", [])}
            picks = []
            for x in q:
                if fh:
                    heads = fh.news(x["t"], 5)
                    x["news"] = [hh["h"] for hh in heads[:3]]
                    own = own_news(heads)
                    if own:
                        x["own"] = own[0]["h"]
                        continue
                picks.append(x)
                if len(picks) >= 5:
                    break
            night["quiet"] = picks
            if picks:
                body = []
                for i, x in enumerate(picks, 1):
                    tr = tiers.get(x["tier"], {})
                    body.append("%d) %s نزل %.1f%% بـ5 جلسات بلا حجم، آخر سعر %.2f، وقف تحت %.2f (درجة %s: ربح %d من 100، معدّل %+.1f%%)" % (
                        i, x["t"], -x["r5"], x["last"], x["low5"] * 0.98, x["tier"], round(tr.get("win", 0.6) * 100), tr.get("avg", 2.0)))
                ring("نزلة هادية عميقة: فكرة لبكرا عالفتحة",
                     "\n".join(body) + "\nالقاعدة: شرا عالفتحة، بيع بعد 5 جلسات عالسكرة. إذا طلع خبر سيء ورا النزلة، الفكرة بتسقط.",
                     priority=4, tags=["chart_with_upwards_trend"], dry=dry)
        else:
            print("no bars for today yet (holiday or late data)")
    else:
        print("no MASSIVE_API_KEY: quiet scan skipped")
    rows = vix_history()
    fs = fear_spike(rows)
    if fs and rows and rows[-1][0] == today:
        lv = ((MEASURED.get("fear_spike_index") or {}).get("by_level") or {}).get("vix40" if fs["level"] == 40 else "vix30") or {}
        night["fear"] = fs
        ring("الخوف سكّر فوق %d لأول مرة بـ20 جلسة" % fs["level"],
             "VIX سكّر %.1f. المقاس من 2000: QQQ بعد 60 جلسة %+.1f%% (ربح %d من 100، أسوأ مرة %+.1f%%). "
             "شرا QQQ بالتقسيط: نص بكرا ونص إذا نزل كمان 5%%. البيع بعد 60 جلسة أو إذا رجع الخوف تحت 20." % (
                 fs["vix"], lv.get("qqq_60d", 4.56), round(lv.get("win", 0.69) * 100), lv.get("worst_60d", -28.6)),
             priority=5, tags=["rotating_light"], dry=dry)
    st = load(os.path.join(STATE_DIR, "state.json"), {})
    night["session"] = {"market": st.get("market"), "rings": st.get("log") if st.get("day") == today else [],
                        "panic_ideas": st.get("panic_ideas") if st.get("day") == today else []}
    os.makedirs(OUT_DIR, exist_ok=True)
    save(os.path.join(OUT_DIR, "day_%s.json" % today), night)
    save(os.path.join(OUT_DIR, "latest.json"), night)
    print("nightly done: quiet %d, fear %s" % (len(night["quiet"]), bool(night["fear"])))
    return 0


def handoff():
    """start the next job of the chain (GitHub allows a workflow to dispatch itself with its own token)"""
    tok, repo = secret("GH_TOKEN"), os.environ.get("GITHUB_REPOSITORY", "")
    if not tok or not repo:
        print("handoff: no token or repository (not on GitHub): skipped")
        return False
    req = urllib.request.Request("https://api.github.com/repos/%s/actions/workflows/eyes.yml/dispatches" % repo,
                                 data=json.dumps({"ref": os.environ.get("GITHUB_REF_NAME", "main"), "inputs": {"mode": "session"}}).encode(),
                                 headers={"User-Agent": UA, "Authorization": "Bearer " + tok, "Accept": "application/vnd.github+json",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            print("handoff: next job started", r.status)
            return True
    except Exception as e:
        print("handoff failed:", str(e).replace(tok, "***"))
        return False


def session(dry=False, every=300, max_minutes=330, sleep=time.sleep):
    """the chain (public repository): stay awake from Monday morning to Friday evening. In the session one check every 5 minutes;
    after the close the nightly work once; otherwise wait. Before GitHub's 6-hour limit the job starts the next one and stops,
    so the eyes never depend on GitHub's schedule (it fired 2 of about 80 planned checks in two days)."""
    t0 = now_ny()
    cp = os.path.join(STATE_DIR, "chain.json")
    passes = 0
    while True:
        n = now_ny()
        hm = (n.hour, n.minute)
        if n.weekday() >= 5 or (n.weekday() == 4 and hm > (19, 30)):
            print("weekend (%s): the chain stops, Monday's schedule starts it again" % t12(n))
            return 0
        if (9, 33) <= hm <= (16, 5):
            try:
                intraday(dry)
            except Exception as e:   # one bad check must not kill the day
                print("check failed:", clean_err(e))
            passes += 1
            wait = every
        elif (EXT_FROM <= hm < (9, 33) or (16, 5) < hm <= EXT_TO) and not (hm >= (18, 15) and load(cp, {}).get("nightly_day") != n.date().isoformat()):
            try:
                extended(dry)
            except Exception as e:
                print("extended check failed:", clean_err(e))
            wait = every
        elif hm >= (18, 15) and load(cp, {}).get("nightly_day") != n.date().isoformat():
            save(cp, {"nightly_day": n.date().isoformat()})
            try:
                nightly(dry)
            except Exception as e:
                print("nightly failed:", clean_err(e))
            wait = 60
        else:
            wait = every if (9, 0) <= hm < (9, 33) else 2 * every
        if (now_ny() - t0).total_seconds() / 60 >= max_minutes:
            print("handing over after %d checks" % passes)
            handoff()
            return 0
        sleep(wait)


def write_day(st):
    save(os.path.join(OUT_DIR, "today.json"), {"day": st.get("day"), "market": st.get("market"), "rings": st.get("log"),
                                               "panic_ideas": st.get("panic_ideas"), "version": VERSION})


# ----------------------------------------------------------------------------------------------------------------- checks
def selftest():
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("ok   " if cond else "FAIL ") + name)
        ok = ok and cond

    check("measured loaded", bool((MEASURED.get("panic_rebound") or {}).get("grades")))
    check("betas loaded", len((BETAS.get("beta") or {})) > 50)
    check("sp500 list", len(lines("sp500.txt")) > 480)
    check("watch list", isinstance(lines("watch.txt"), list))   # a public repository keeps the list in the WATCHLIST secret
    # wave: SPY -4%, beta 1.5 -> limit -9%: -12% is not wave; beta 2.5 -> -15% limit: -12% is wave
    BETAS.setdefault("beta", {})["ZZZ"] = 2.5
    check("wave by beta", is_wave(-12, "ZZZ", -4, -3)[0])
    check("not wave", not is_wave(-12, "YYY", -4, -3)[0])
    check("chips wave", is_wave(-12, "NVDA", -1, -8)[0] or "NVDA" not in (BETAS.get("chips") or []))
    check("grade A at VIX 35", panic_grade(35)["id"] == "A")
    check("grade B at VIX 25", panic_grade(25)["id"] == "B")
    check("own news", bool(own_news([{"h": "Acme cuts guidance after weak quarter"}])) and not own_news([{"h": "Stocks slide as yields jump"}]))
    # quiet scan on a made-up history: 210 days up slowly, then -16% in 5 quiet days
    ds = ["d%03d" % i for i in range(212)]
    c = [90.0] * 206 + [118, 112, 108, 104, 100.5, 99.0]
    v = [1000.0] * 212
    hq = {"dates": ds, "c": {"QQQ1": c}, "v": {"QQQ1": v}}
    got = quiet_scan(hq, ["QQQ1"])
    check("quiet found", bool(got) and got[0]["tier"] in ("A", "B"))
    hq["v"]["QQQ1"] = [1000.0] * 209 + [5000.0, 1000.0, 1000.0]
    check("loud drop skipped", not quiet_scan(hq, ["QQQ1"]))
    rows = [("x%d" % i, 18.0) for i in range(30)] + [("today", 31.0)]
    check("fear spike", (fear_spike(rows) or {}).get("level") == 30)
    rows2 = rows[:-5] + [("y", 32.0)] + rows[-4:]
    check("not first time", fear_spike(rows2) is None)
    check("12h time", t12(dt.datetime(2026, 11, 5, 14, 45)) == "2:45 بعد الضهر" and t12(dt.datetime(2026, 11, 5, 9, 5)) == "9:05 الصبح")
    check("token hidden", "SECRET123" not in clean_err("http://x?token=SECRET123&a=1"))
    # the chain: 3 checks before the close (one fails and the day goes on), the nightly work once, then the hand-over
    check("critical headline", bool(CRIT_RX.search("Nvidia CEO Jensen Huang resigns, CFO named interim chief")) and bool(CRIT_RX.search("Acme cuts guidance")) and not CRIT_RX.search("Nvidia stock rises with chip rally"))
    # holdings watch with a fake data source: -13% rings the 12% step once, a critical headline rings once
    class FakeFH:
        calls = 0
        def quote(self, t):
            return {"sym": t, "last": 151.0, "prev": 173.7, "pct": -13.1, "high": 160.0, "low": 148.0}
        def news(self, t, d=1):
            return [{"h": "Nvidia CEO Jensen Huang resigns effective immediately", "src": "Reuters", "at": time.time() - 600},
                    {"h": "Nvidia shares in focus", "src": "X", "at": time.time() - 600}]
    os.environ["HOLDINGS"] = "NVDA"
    rings, stt = [], {"sent": {}}
    def fonce(k, *a, **kw):
        if k in stt["sent"]:
            return False
        stt["sent"][k] = 1; rings.append((k, kw.get("priority"))); return True
    nn = dt.datetime(2026, 11, 12, 10, 0)
    holdings_watch(FakeFH(), stt, nn, fonce, in_session=True)
    holdings_watch(FakeFH(), stt, nn, fonce, in_session=True)
    os.environ.pop("HOLDINGS", None)
    check("holdings: step 12 once, loud", [r for r in rings if r[0].startswith("hstep:NVDA:d:12")] == [("hstep:NVDA:d:12", 5)])
    pl_own = plan_line({"sym": "NVDA", "pct": -13.1, "last": 151.0, "low": 148.0}, [{"h": "Nvidia CEO resigns"}], True, -0.3, -2.0)
    pl_wave = plan_line({"sym": "ZZZ", "pct": -12.0, "last": 50.0, "low": 49.0}, [{"h": "Stocks slide as yields jump"}], False, -4.0, -3.0)
    pl_up = plan_line({"sym": "CRWV", "pct": 24.7, "last": 60.0}, [], True, 1.0, 6.0)
    check("plan: own news = no entry, exit by size (no near stop)", pl_own.startswith("دخول: لا.") and "خفّف للحد" in pl_own and "بلا وقف قريب" in pl_own)
    check("plan: wave fall = entry near the close, exit tomorrow", "دخول: قرب السكرة" in pl_wave and "50.00" in pl_wave and "سكرة بكرا" in pl_wave)
    check("plan: big rise = do not chase", pl_up.startswith("دخول: لا تلحق") and "خفّف للحد" in pl_up)
    check("holdings: one ring for the story (the step carries the headline)", len(rings) == 1 and not [r for r in rings if r[0].startswith("hnews:")])
    rings2, st2 = [], {"sent": {}}
    def fonce2(k, *a, **kw):
        if k in st2["sent"]:
            return False
        st2["sent"][k] = 1; rings2.append((k, kw.get("priority"))); return True
    os.environ["HOLDINGS"] = "NVDA"
    holdings_watch(FakeFH(), st2, nn, fonce2, in_session=False)   # before the open: no price, the headline rings alone
    os.environ.pop("HOLDINGS", None)
    check("holdings: critical news before the open rings loud", [p for k, p in rings2 if k.startswith("hnews:")] == [5])
    global intraday, now_ny, nightly, STATE_DIR, extended
    import tempfile
    keep = (intraday, now_ny, nightly, STATE_DIR, extended)
    clock, calls, nights = [dt.datetime(2026, 11, 10, 15, 52)], [], []
    def fake_intraday(dry=False):
        calls.append(clock[0])
        if len(calls) == 2:
            raise RuntimeError("boom")
        return 0
    exts = []
    intraday, now_ny, nightly, STATE_DIR = fake_intraday, (lambda: clock[0]), (lambda dry=False: nights.append(clock[0]) or 0), tempfile.mkdtemp()
    extended = lambda dry=False: exts.append(clock[0]) or 0
    try:
        rc = session(dry=True, sleep=lambda sec: clock.__setitem__(0, clock[0] + dt.timedelta(seconds=sec)))
        clock[0] = dt.datetime(2026, 11, 14, 11, 0)   # a Saturday
        rc2 = session(dry=True, sleep=lambda sec: None)
    finally:
        intraday, now_ny, nightly, STATE_DIR, extended = keep
    check("chain: session checks", rc == 0 and len(calls) == 3)
    check("chain: nightly once", len(nights) == 1 and nights[0].hour == 18)
    check("chain: holdings news after the close", len(exts) > 5 and all((x.hour, x.minute) > (16, 5) for x in exts))
    check("chain: hand-over on time", (clock[0].year, rc2) == (2026, 0))
    print("SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    mode = (argv[1] if len(argv) > 1 else "intraday").lower()
    dry = "--dry" in argv
    if mode == "intraday":
        return intraday(dry)
    if mode == "nightly":
        return nightly(dry)
    if mode == "session":
        return session(dry)
    if mode == "test":
        n = now_ny()
        ok = ring("تجربة: العيون موصولة (%s بتوقيت نيويورك)" % t12(n),
                  "إذا وصلك هالجرس بصوت عالي، كل شي جاهز. وقت الذعر رح يجيك متله، ومعه الأسهم والسعر والوقف.", priority=5,
                  tags=["white_check_mark"], dry=dry)
        return 0 if ok or dry else 1
    if mode == "selftest":
        return selftest()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
