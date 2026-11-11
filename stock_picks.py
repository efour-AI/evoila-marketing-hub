"""Weekly stock-pick contest for the marketing hub (Airtable table Stock Picks).

At the end of the Wednesday marketing call each person names one stock. This job:
  1. Reads the newest weekly marketing call ("Marketing and Sales Weekly Sync", usually Wednesday) in Call Transcripts that has no picks yet, has Claude pull out
     who picked which ticker, checks each ticker exists, and adds one Stock Picks row per person.
  2. Locks the start price: the price at 2:30 PM Central on the call day (Week of) (Status -> Live).
  3. Locks the end price: the price at 1:00 PM Central seven days later, right before the next call
     (Status -> Final).
The hub shows live prices for Live rows itself (Make scenario "evoila Hub Stocks").

Fix a misheard pick by editing Ticker on the row (and clearing Start price if it was already locked).
Status "Check this" means the job could not find the ticker or a price; the reason is in Notes.

Runs from .github/workflows/stock-picks.yml (Wednesday afternoons, daily backstop, or by hand).
Public repository: never print transcript text to the log.
"""
import datetime as dt
import json
import re
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

import ai_steps
from hub_jobs import BASE, C, T_CALL, api, rows

CT = ZoneInfo("America/Chicago")
T_PICK = "tblBbD5cXBJtKoyC5"
F = {"pick": "fldFmqm0N7IqXHcIC", "week": "fldp1MSIyrZkgYvzf", "person": "fld06JErZcpEV3FaA", "ticker": "fldjN6YRilOn9OYfC",
     "company": "fldbdD7BEq9T4svBe", "start": "fldRgOxet2uwIunMk", "start_t": "fldnIdGA7fNTj6Wni", "end": "fld4zsHPhepkEPZUw",
     "end_t": "fldlZUxt6R6Fp0MSV", "status": "fldUTYIOWC0mPDZYp", "source": "fldwgqdOGk6YqEk5C", "notes": "fldY9SLaihVJZPdTb"}
PEOPLE = ["Kevin O'Brien", "Glen Tindal", "Mark Maisano", "Andrew Hald", "Tom MacKay", "Julie Tindal"]
START_AT, END_AT = (14, 30), (13, 0)          # Central time: start on the call day, end the next Wednesday
UA = {"User-Agent": "Mozilla/5.0 (evoila marketing hub)"}

PROMPT = """Below is the end of the transcript of evoila's weekly marketing call. Near the end, people play a game:
each person names one stock they think will go up the most over the next week.

List the picks. Rules:
- Only the stock each person finally committed to for this week's game. Ignore stocks that were only discussed,
  joked about, or were someone's pick from an earlier week.
- person must be one of: {people}. Match first names (Kevin, Glen, Mark, Andrew, Tom, Julie).
- ticker is the US exchange symbol in capitals. People often say the company name, spell the letters, or the
  transcript mishears them (for example "Vertiv" is VRT, "Rocket Lab" is RKLB, "Lumentum" is LITE). Give your best
  ticker and the company name.
- If someone made a pick for a person who was absent, include it under the absent person.
Reply with JSON only, no code fences: {{"picks": [{{"person": "...", "ticker": "...", "company": "..."}}]}}
If there are no picks in this transcript, reply {{"picks": []}}.

MEETING: {title} ({date})

TRANSCRIPT (end of call):
{text}"""


# ---------- prices ----------
def _get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
        return json.loads(r.read())


def price_at(ticker, when):
    """Last traded price at or before `when` (aware datetime). Returns (price, bar_time) or (None, None)."""
    t = int(when.timestamp())
    for interval, back in (("1m", 3), ("5m", 6), ("1d", 10)):
        span = {"1m": 60, "5m": 300, "1d": 86400}[interval]
        q = urllib.parse.urlencode({"period1": t - back * 86400, "period2": t + 3600, "interval": interval,
                                    "includePrePost": "false"})
        try:
            res = _get(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ticker)}?{q}")["chart"]["result"][0]
        except Exception:
            continue
        ts = res.get("timestamp") or []
        closes = ((res.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
        best = None
        for s, c in zip(ts, closes):
            if c is not None and s + span <= t + 1:        # bar finished by `when`
                best = (round(float(c), 4), s + span)
        if best:
            return best[0], dt.datetime.fromtimestamp(min(best[1], t), dt.timezone.utc)
    return None, None


def company_name(ticker):
    try:
        meta = _get(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ticker)}?range=1d&interval=1d")["chart"]["result"][0]["meta"]
        return meta.get("longName") or meta.get("shortName") or ""
    except Exception:
        return None                                       # unknown ticker


# ---------- picks from the call ----------
def at(day, hm):
    return dt.datetime.combine(day, dt.time(*hm), CT)


def extract_from_calls():
    have = rows(T_PICK)
    used = {r["fields"].get(F["source"], "") for r in have}
    weeks = {r["fields"].get(F["week"]) for r in have}
    taken = {(r["fields"].get(F["week"]), r["fields"].get(F["person"])) for r in have}
    calls = rows(T_CALL, "AND({Full Transcript}!='', IS_AFTER({Meeting Date}, DATEADD(TODAY(), -3, 'days')))")
    added = 0
    for c in sorted(calls, key=lambda r: r["fields"].get(C["date"], "")):
        f = c["fields"]
        title, day = str(f.get(C["title"], "")), str(f.get(C["date"], ""))[:10]
        # the weekly marketing call (usually Wednesday); skip it once that day already has picks
        if not day or not re.search(r"marketing", title, re.I) or c["id"] in used or day in weeks:
            continue
        text = str(f.get(C["text"], ""))
        prompt = PROMPT.format(people=", ".join(PEOPLE), title=title, date=day, text=text[-30000:])
        try:
            resp = ai_steps._call({"model": ai_steps.MODEL, "max_tokens": 800,
                                   "messages": [{"role": "user", "content": prompt}]}, timeout=120)
            out = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
            picks = json.loads(out[out.find("{"):out.rfind("}") + 1]).get("picks", [])
        except Exception as e:
            print("pick extraction failed:", type(e).__name__)
            continue
        recs = []
        for p in picks:
            who = next((n for n in PEOPLE if n.split()[0].lower() == str(p.get("person", "")).split(" ")[0].lower()), None)
            tk = re.sub(r"[^A-Z0-9.\-]", "", str(p.get("ticker", "")).upper())[:10]
            if not who or not tk or (day, who) in taken:
                continue
            name = company_name(tk)
            ok = name is not None
            recs.append({"fields": {F["pick"]: f"{day} · {who.split()[0]} · {tk}", F["week"]: day, F["person"]: who,
                                    F["ticker"]: tk, F["company"]: name or str(p.get("company", ""))[:80],
                                    F["status"]: "Waiting for start price" if ok else "Check this",
                                    F["source"]: c["id"],
                                    F["notes"]: f"From the call notes: {title}" + ("" if ok else f". Ticker {tk} not found, fix it here.")}})
            taken.add((day, who))
        if not recs:   # mark the call as read so it isn't asked again
            print(f"no picks found in the {day} call")
            continue
        for i in range(0, len(recs), 10):
            api("POST", f"https://api.airtable.com/v0/{BASE}/{T_PICK}", {"records": recs[i:i + 10], "typecast": True})
        added += len(recs)
    print(f"picks added: {added}")


# ---------- lock prices ----------
def lock_prices():
    now = dt.datetime.now(dt.timezone.utc)
    upd = []
    for r in rows(T_PICK, "AND({Ticker}!='', {Week of}!='', {Status}!='Final')"):
        f = r["fields"]
        tk = str(f.get(F["ticker"], "")).strip().upper()
        day = dt.date.fromisoformat(f[F["week"]][:10])
        start, end = at(day, START_AT), at(day + dt.timedelta(days=7), END_AT)
        ch = {}
        sp = f.get(F["start"])
        if sp is None and now >= start + dt.timedelta(minutes=2):
            p, when = price_at(tk, start)
            if p is None:
                ch = {F["status"]: "Check this", F["notes"]: f"No price found for {tk}. Check the ticker."}
            else:
                sp = p
                ch = {F["start"]: p, F["start_t"]: when.isoformat(), F["status"]: "Live"}
                if not f.get(F["company"]):
                    ch[F["company"]] = company_name(tk) or ""
        if sp is not None and now >= end + dt.timedelta(minutes=2):
            p, when = price_at(tk, end)
            if p is not None:
                ch.update({F["end"]: p, F["end_t"]: when.isoformat(), F["status"]: "Final"})
        elif sp is not None and f.get(F["status"]) != "Live" and F["status"] not in ch:
            ch[F["status"]] = "Live"
        if ch:
            upd.append({"id": r["id"], "fields": ch})
    for i in range(0, len(upd), 10):
        api("PATCH", f"https://api.airtable.com/v0/{BASE}/{T_PICK}", {"records": upd[i:i + 10], "typecast": True})
    print(f"rows updated: {len(upd)}")


def run():
    extract_from_calls()
    lock_prices()


if __name__ == "__main__":
    run()
