#!/usr/bin/env python
"""Scrape horse racing results from sportinglife.com.

Each results page embeds its data as Next.js JSON (<script id="__NEXT_DATA__">),
so no HTML parsing is needed. Raw race JSON is cached under <out>/raw/ so runs
can be resumed and re-exported without re-fetching.

Examples:
    python sl_results.py                          # today's results
    python sl_results.py --date 2026-10-12
    python sl_results.py --from 2026-10-01 --to 2026-10-07 --uk-ire
    python sl_results.py --days 7                 # last 7 days (incl. today)
"""

import argparse
import csv
import json
import logging
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

BASE = "https://www.sportinglife.com"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
UK_IRE = {"England", "Scotland", "Wales", "Northern Ireland", "Ireland", "Eire"}
UK_TZ = ZoneInfo("Europe/London")
# Races in these stages have a settled result and are safe to cache.
FINAL_STAGES = {"WEIGHEDIN", "RESULT", "ABANDONED"}
# French trotting races are named "... - Attele" (harness) or "... - Monte" (ridden).
TROT_RE = re.compile(r"-\s*(Attel[eé]|Mont[eé])\b", re.I)

log = logging.getLogger("sl_results")


class Client:
    def __init__(self, delay=1.0, retries=3):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "en-GB"})
        self.delay = delay
        self.retries = retries
        self._last = 0.0

    def page_props(self, path):
        for attempt in range(1, self.retries + 1):
            wait = self.delay - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                r = self.s.get(BASE + path, timeout=30)
                r.raise_for_status()
                m = NEXT_DATA_RE.search(r.text)
                if not m:
                    raise ValueError("no __NEXT_DATA__ in page")
                return json.loads(m.group(1))["props"]["pageProps"]
            except (requests.RequestException, ValueError, KeyError, TypeError) as e:
                log.warning("%s failed (attempt %d/%d): %s", path, attempt, self.retries, e)
                time.sleep(2 ** attempt)
        raise RuntimeError(f"giving up on {path}")


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def day_races(client, day, uk_ire_only, intl_pattern_only=False):
    """Yield (meeting, race_summary) for every race on the given day.

    intl_pattern_only keeps every UK/Irish race but only Group/Grade/Listed races elsewhere,
    excluding trotting races.
    """
    pp = client.page_props(f"/racing/results/{day.isoformat()}")
    for meeting in pp.get("meetings") or []:
        ms = meeting["meeting_summary"]
        country = ms["course"].get("country", {}).get("long_name", "")
        if uk_ire_only and country not in UK_IRE:
            continue
        for race in meeting.get("races", []):
            if (intl_pattern_only and country not in UK_IRE
                    and (not rp_pattern(race.get("name")) or TROT_RE.search(race.get("name") or ""))):
                continue
            yield ms, race


def fetch_race(client, raw_dir, ms, rs, refresh):
    """Return race JSON, from cache when available."""
    race_id = rs["race_summary_reference"]["id"]
    cache = raw_dir / rs["date"] / f"{race_id}.json"
    if cache.exists() and not refresh:
        return json.loads(cache.read_text())
    path = f"/racing/results/{rs['date']}/{slugify(rs['course_name'])}/{race_id}/{slugify(rs['name'])}"
    pp = client.page_props(path)
    race = pp.get("race")
    if not race:
        log.warning("no race data for %s", path)
        return None
    race["_meeting"] = ms
    if race["race_summary"].get("race_stage") in FINAL_STAGES:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(race))
    return race


BREEDING = ("sire", "dam", "damsire")


def fetch_breeding(client, raw_dir, horse_id):
    """Return {sire, dam, damsire} names from the horse's profile page, cached for good."""
    cache = raw_dir / "horses" / f"{horse_id}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    profile = client.page_props(f"/racing/profiles/horse/{horse_id}").get("profile") or {}
    breeding = {k: (profile.get(k) or {}).get("name") for k in BREEDING}
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(breeding))
    return breeding


RACE_FIELDS = [
    "race_id", "date", "course", "country", "time", "off_time", "race_name", "race_class",
    "age", "distance", "going", "surface", "handicap", "runners", "winning_time", "stage",
    "winner_prize", "tote_win", "tote_place", "exacta", "trifecta", "tricast",
    "straight_forecast", "swingers", "placepot", "quadpot", "book_percentage", "stewards",
]
# Racing Post (rpscrape) style versions of fields, for joining with RP-derived spreadsheets.
RP_RACE_FIELDS = [
    "rp_course", "rp_type", "rp_class", "rp_pattern", "rp_rating_band", "rp_age_band",
    "rp_sex_rest", "rp_dist", "rp_going", "ran",
]
RUNNER_FIELDS = [
    "race_id", "date", "course", "time", "race_name", "position", "status", "beaten",
    "horse", "horse_id", "horse_age", "sex", "sire", "dam", "damsire", "cloth", "draw", "weight", "headgear",
    "official_rating", "jockey", "jockey_claim", "trainer", "owner", "sp", "bet_movements",
    "comment",
]
RP_RUNNER_FIELDS = ["rp_pos", "rp_ovr_btn", "rp_btn", "rp_sex", "rp_hg", "rp_time", "rp_prize"]


def uk_time(day, hhmm):
    """Sporting Life stores times in UTC; convert 'HH:MM[:SS]' to UK local time."""
    if not hhmm:
        return hhmm
    fmt = "%Y-%m-%d %H:%M:%S" if hhmm.count(":") == 2 else "%Y-%m-%d %H:%M"
    try:
        t = datetime.strptime(f"{day} {hhmm}", fmt).replace(tzinfo=timezone.utc)
    except ValueError:
        return hhmm
    return t.astimezone(UK_TZ).strftime(fmt.split(" ")[1])


# Racing Post style conversions ----------------------------------------------

LENGTHS = {"dh": 0, "dht": 0, "nse": 0.05, "sh": 0.1, "shd": 0.1, "hd": 0.2, "snk": 0.25,
           "nk": 0.3, "dist": 30}
FRACTIONS = {"¼": 0.25, "½": 0.5, "¾": 0.75}
CASUALTY_CODES = {"PulledUp": "PU", "Fell": "F", "UnseatedRider": "UR", "Unseated": "UR",
                  "RefusedToRace": "RR", "Refused": "REF", "BroughtDown": "BD",
                  "CarriedOut": "CO", "SlippedUp": "SU", "RanOut": "RO", "Disqualified": "DSQ",
                  "LeftAtStart": "LFT"}
NON_RUNNER = {"NONRUNNER", "WITHDRAWN"}


def lengths(text):
    """'1 ¾' -> 1.75, 'nk' -> 0.3; None if unparseable."""
    if not text:
        return None
    t = text.strip().lower()
    if t in LENGTHS:
        return LENGTHS[t]
    total = 0.0
    for part in t.split():
        if part in FRACTIONS:
            total += FRACTIONS[part]
        elif part[-1] in FRACTIONS and part[:-1].isdigit():
            total += int(part[:-1]) + FRACTIONS[part[-1]]
        else:
            try:
                total += float(part)
            except ValueError:
                return None
    return total


def rp_dist(text):
    """'1m 208y' -> '1m1f': nearest half furlong, Racing Post style."""
    m = re.fullmatch(r"\s*(?:(\d+)m)?\s*(?:(\d+)f)?\s*(?:(\d+)y)?\s*", text or "")
    if not m or not any(m.groups()):
        return text
    miles, furlongs, yards = (int(g or 0) for g in m.groups())
    f = round((miles * 1760 + furlongs * 220 + yards) / 110) / 2
    miles, rem = int(f // 8), f % 8
    out = f"{miles}m" if miles else ""
    if rem:
        whole = int(rem)
        out += (str(whole) if whole else "") + ("½" if rem - whole else "") + "f"
    return out


def rp_going(text):
    if not text:
        return text
    text = re.sub(r"\s*\(.*?\)", "", text).replace(" / ", " to ")
    return " ".join(w.capitalize() for w in text.split())


def rp_age_band(text):
    t = text or ""
    for pat, rep in [(r"(\d+)YO plus", r"\1yo+"), (r"(\d+)YO only", r"\1yo"),
                     (r"(\d+)YO to (\d+)YO", r"\1-\2yo")]:
        if re.fullmatch(pat, t, re.I):
            return re.sub(pat, rep, t, flags=re.I)
    return t.lower()


def rp_type(name):
    n = (name or "").lower()
    if "chase" in n:
        return "Chase"
    if "hurdle" in n:
        return "Hurdle"
    if re.search(r"national hunt flat|\bn\.?h\.? flat|\binh flat|bumper", n):
        return "NH Flat"
    return "Flat"


def rp_pattern(name):
    m = re.search(r"\b(Group|Grade)\s*([123])\b", name or "", re.I)
    if m:
        return f"{m.group(1).capitalize()} {m.group(2)}"
    return "Listed" if re.search(r"\blisted\b", name or "", re.I) else ""


def rp_sex_rest(name):
    n = (name or "").lower()
    for pat, code in [(r"fillies'? (and|&) mares", "F & M"), (r"colts'? (and|&) geldings", "C & G"),
                      (r"\bfillies", "F"), (r"\bmares", "M"), (r"\bcolts", "C"), (r"\bgeldings", "G")]:
        if re.search(pat, n):
            return code
    return ""


def rp_course(name, surface, country):
    out = name or ""
    if surface and surface != "TURF":
        out += " (AW)"
    if country == "Eire":
        out += " (IRE)"
    return out


def rp_time(text):
    """'1m 54.21s' -> '1:54.21'."""
    m = re.fullmatch(r"\s*(?:(\d+)m)?\s*([\d.]+)s\s*", text or "")
    if not m:
        return text
    return f"{int(m.group(1) or 0)}:{float(m.group(2)):05.2f}"


def money(text):
    """'3477.0 GBP' -> '3477'."""
    m = re.match(r"\s*([\d.]+)", text or "")
    if not m:
        return ""
    v = float(m.group(1))
    return str(int(v)) if v.is_integer() else f"{v:.2f}"


def race_row(race):
    rs, ms = race["race_summary"], race.get("_meeting", {})
    prizes = (race.get("prizes") or {}).get("prize") or []
    return {
        "race_id": rs["race_summary_reference"]["id"],
        "date": rs.get("date"),
        "course": rs.get("course_name"),
        "country": ms.get("course", {}).get("country", {}).get("long_name"),
        "time": uk_time(rs.get("date"), rs.get("time")),
        "off_time": uk_time(rs.get("date"), rs.get("off_time")),
        "race_name": rs.get("name"),
        "race_class": rs.get("race_class"),
        "age": rs.get("age"),
        "distance": rs.get("distance"),
        "going": rs.get("going"),
        "surface": (rs.get("course_surface") or {}).get("surface"),
        "handicap": rs.get("has_handicap"),
        "runners": rs.get("ride_count"),
        "winning_time": rs.get("winning_time"),
        "stage": rs.get("race_stage"),
        "winner_prize": next((p["prize"] for p in prizes if p.get("position") == 1), None),
        "tote_win": race.get("tote_win"),
        "tote_place": race.get("place_win"),
        "exacta": race.get("exacta_win"),
        "trifecta": race.get("trifecta"),
        "tricast": race.get("tricast"),
        "straight_forecast": race.get("straight_forecast"),
        "swingers": race.get("swingers"),
        "placepot": (race.get("place_pot") or {}).get("pot"),
        "quadpot": (race.get("quad_pot") or {}).get("pot"),
        "book_percentage": race.get("on_course_book_percentage"),
        "stewards": race.get("stewards"),
        "rp_course": rp_course(rs.get("course_name"), (rs.get("course_surface") or {}).get("surface"),
                               ms.get("course", {}).get("country", {}).get("long_name")),
        "rp_type": rp_type(rs.get("name")),
        "rp_class": f"Class {rs['race_class']}" if rs.get("race_class") else "",
        "rp_pattern": rp_pattern(rs.get("name")),
        "rp_rating_band": (re.search(r"\((\d+-\d+)\)", rs.get("name") or "") or [None, ""])[1],
        "rp_age_band": rp_age_band(rs.get("age")),
        "rp_sex_rest": rp_sex_rest(rs.get("name")),
        "rp_dist": rp_dist(rs.get("distance")),
        "rp_going": rp_going(rs.get("going")),
        "ran": sum(1 for r in race.get("rides") or [] if r.get("ride_status") not in NON_RUNNER),
    }


def runner_rows(race):
    rs = race["race_summary"]
    rides = sorted(race.get("rides") or [], key=lambda r: (r.get("finish_position") or 999))
    prizes = {p.get("position"): money(p.get("prize"))
              for p in (race.get("prizes") or {}).get("prize") or []}
    ovr = 0.0
    for ride in rides:
        horse = ride.get("horse") or {}
        pos = ride.get("finish_position") or 0
        if ride.get("ride_status") in NON_RUNNER:
            rp_pos, btn = "NR", None
        elif pos:
            rp_pos = str(pos)
            btn = 0.0 if pos == 1 else lengths(ride.get("finish_distance"))
            ovr = 0.0 if pos == 1 else ovr + (btn or 0)
        else:
            reason = (ride.get("casualty") or {}).get("reason", "")
            rp_pos, btn = CASUALTY_CODES.get(reason, reason or "-"), None
        yield {
            "race_id": rs["race_summary_reference"]["id"],
            "date": rs.get("date"),
            "course": rs.get("course_name"),
            "time": uk_time(rs.get("date"), rs.get("time")),
            "race_name": rs.get("name"),
            "position": ride.get("finish_position") or None,
            "status": ride.get("ride_status"),
            "beaten": ride.get("finish_distance"),
            "horse": horse.get("name"),
            "horse_id": (horse.get("horse_reference") or {}).get("id"),
            "horse_age": horse.get("age"),
            "sex": (horse.get("sex") or {}).get("type"),
            "cloth": ride.get("cloth_number"),
            "draw": ride.get("draw_number"),
            "weight": ride.get("handicap"),
            "headgear": ",".join(h.get("symbol", "") for h in ride.get("headgear") or []),
            "official_rating": ride.get("official_rating"),
            "jockey": (ride.get("jockey") or {}).get("name"),
            "jockey_claim": ride.get("jockey_claim"),
            "trainer": (ride.get("trainer") or {}).get("name"),
            "owner": (ride.get("owner") or {}).get("name"),
            "sp": (ride.get("betting") or {}).get("current_odds"),
            "bet_movements": ride.get("bet_movements"),
            "comment": ride.get("ride_description"),
            "rp_pos": rp_pos,
            "rp_ovr_btn": "" if btn is None else f"{ovr:g}" if pos != 1 else "0",
            "rp_btn": "" if btn is None else f"{btn:g}",
            "rp_sex": ((horse.get("sex") or {}).get("type") or "").upper(),
            "rp_hg": "".join(h.get("symbol", "") for h in ride.get("headgear") or []),
            "rp_time": rp_time(rs.get("winning_time")) if pos == 1 else "",
            "rp_prize": prizes.get(pos, "") if pos else "",
        }


def daterange(start, end):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def parse_args(argv):
    p = argparse.ArgumentParser(description="Scrape racing results from sportinglife.com")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--date", type=date.fromisoformat, help="single date (YYYY-MM-DD)")
    g.add_argument("--days", type=int, help="last N days including today")
    p.add_argument("--from", dest="start", type=date.fromisoformat, help="start date (YYYY-MM-DD)")
    p.add_argument("--to", dest="end", type=date.fromisoformat, help="end date (default: today)")
    p.add_argument("--uk-ire", action="store_true", help="only UK and Irish meetings")
    p.add_argument("--intl-pattern", action="store_true",
                   help="outside UK/Ireland, only Group/Grade/Listed races (no trotting)")
    p.add_argument("--out", type=Path, default=Path("data"), help="output directory (default: data)")
    p.add_argument("--delay", type=float, default=1.0, help="seconds between requests (default: 1.0)")
    p.add_argument("--refresh", action="store_true", help="ignore cached race JSON")
    p.add_argument("--no-breeding", dest="breeding", action="store_false",
                   help="skip fetching sire/dam/damsire (one extra request per new horse)")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)

    today = date.today()
    if a.date:
        a.start = a.end = a.date
    elif a.days:
        a.start, a.end = today - timedelta(days=a.days - 1), today
    else:
        a.end = a.end or today
        a.start = a.start or a.end
    if a.start > a.end:
        p.error("--from must not be after --to")
    return a


def scrape(start, end, out, uk_ire=False, delay=1.0, refresh=False, breeding=True,
           intl_pattern=False, on_race=None, on_day=None, stop=None):
    """Scrape start..end into races_/runners_ CSVs under out; return (races_path, runners_path).

    breeding fetches each runner's sire/dam/damsire from its profile page (one extra request
    per horse the first time it is seen; cached under raw/horses/ after that).
    intl_pattern keeps only Group/Grade/Listed races (no trotting) from meetings outside the
    UK and Ireland.

    on_race(race_row, runner_rows) is called per race, on_day(index, total, day) per day,
    and scraping stops early if stop (a threading.Event) gets set.
    """
    client = Client(delay=delay)
    raw_dir = out / "raw"
    out.mkdir(parents=True, exist_ok=True)

    tag = start.isoformat() if start == end else f"{start}_{end}"
    races_path, runners_path = out / f"races_{tag}.csv", out / f"runners_{tag}.csv"
    days = list(daterange(start, end))
    n_races = n_runners = 0
    with open(races_path, "w", newline="") as fr, open(runners_path, "w", newline="") as fu:
        races_w = csv.DictWriter(fr, RACE_FIELDS, extrasaction="ignore")
        runners_w = csv.DictWriter(fu, RUNNER_FIELDS, extrasaction="ignore")
        races_w.writeheader()
        runners_w.writeheader()
        for i, day in enumerate(days):
            if stop and stop.is_set():
                log.info("stopped")
                break
            if on_day:
                on_day(i, len(days), day)
            try:
                summaries = list(day_races(client, day, uk_ire, intl_pattern))
            except RuntimeError as e:
                log.error("%s: %s", day, e)
                continue
            log.info("%s: %d races", day, len(summaries))
            for ms, rs in summaries:
                if stop and stop.is_set():
                    break
                try:
                    race = fetch_race(client, raw_dir, ms, rs, refresh)
                except RuntimeError as e:
                    log.error("%s", e)
                    continue
                if not race:
                    continue
                rrow, urows = race_row(race), list(runner_rows(race))
                if breeding:
                    for u in urows:
                        if stop and stop.is_set():
                            break
                        if not u["horse_id"]:
                            continue
                        try:
                            u.update(fetch_breeding(client, raw_dir, u["horse_id"]))
                        except RuntimeError as e:
                            log.error("breeding for %s: %s", u["horse"], e)
                races_w.writerow(rrow)
                runners_w.writerows(urows)
                n_races += 1
                n_runners += len(urows)
                if on_race:
                    on_race(rrow, urows)
            fr.flush()
            fu.flush()

    log.info("wrote %d races -> %s", n_races, races_path)
    log.info("wrote %d runners -> %s", n_runners, runners_path)
    return races_path, runners_path


def main(argv=None):
    a = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    scrape(a.start, a.end, a.out, uk_ire=a.uk_ire, delay=a.delay, refresh=a.refresh,
           breeding=a.breeding, intl_pattern=a.intl_pattern)


if __name__ == "__main__":
    sys.exit(main())
