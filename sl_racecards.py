#!/usr/bin/env python
"""Scrape horse racing racecards (today's, tomorrow's or a few days ahead) from sportinglife.com.

Like the results pages, each racecard page embeds its data as Next.js JSON, so this reuses
sl_results' Client and Racing Post-style helpers. Cards change up to the off (non-runners,
odds), so race JSON is not cached; horse breeding is, under <out>/raw/horses/.

Examples:
    python sl_racecards.py                        # today's cards
    python sl_racecards.py --tomorrow --uk-ire
    python sl_racecards.py --date 2026-10-22
"""

import argparse
import csv
import html
import logging
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import sl_results
from sl_results import Client, slugify, uk_time, money, UK_IRE, NON_RUNNER

log = logging.getLogger("sl_racecards")

# How many past runs to flatten into last1_* .. lastN_* runner columns.
LAST_RUNS = 3
STAT_CODES = {"Course": "C", "Distance": "D", "CourseDistance": "CD", "BeatenFavourite": "BF"}

RACE_FIELDS = [
    "race_id", "date", "course", "country", "time", "race_name", "race_class", "age", "distance",
    "going", "surface", "handicap", "runners", "declared", "places", "stage", "prize_fund",
    "winner_prize", "prizes", "betting_forecast", "book_percentage", "verdict", "verdict_1st",
    "verdict_2nd", "verdict_3rd", "last_year_winner", "last_year_trainer", "last_year_jockey",
    "last_year_weight", "last_year_odds", "url",
]
RP_RACE_FIELDS = [f for f in sl_results.RP_RACE_FIELDS if f != "ran"]
LAST_RUN_FIELDS = [f"last{i}_{k}" for i in range(1, LAST_RUNS + 1)
                   for k in ("date", "course", "pos", "ran", "dist", "going", "class", "weight",
                             "odds", "comment")]
RUNNER_FIELDS = [
    "race_id", "date", "course", "time", "race_name", "cloth", "draw", "horse", "horse_id",
    "horse_age", "sex", "colour", "foaled", "sire", "dam", "damsire", "form", "last_ran_days",
    "weight", "headgear", "headgear_first_time", "official_rating", "jockey", "jockey_claim",
    "trainer", "owner", "status", "odds", "forecast_odds", "best_odds", "best_odds_decimal",
    "best_odds_bookie", "timeform_stars", "sl_rating", "verdict_pick", "stats", "runs", "wins",
    "placed", "commentary",
] + LAST_RUN_FIELDS
RP_RUNNER_FIELDS = ["rp_sex", "rp_hg"]


def card_path(day):
    """The site serves today's and tomorrow's cards from fixed paths; later days by date."""
    today = date.today()
    if day == today:
        return "/racing/racecards"
    if day == today + timedelta(days=1):
        return "/racing/racecards/tomorrow"
    return f"/racing/racecards/{day.isoformat()}"


def day_races(client, day, uk_ire_only):
    """Yield (meeting_summary, race_summary) for every race on the day's racecards."""
    pp = client.page_props(card_path(day))
    for meeting in pp.get("meetings") or []:
        ms = meeting["meeting_summary"]
        if ms.get("date") != day.isoformat():
            log.warning("%s: site returned cards for %s", day, ms.get("date"))
            return
        country = ms["course"].get("country", {}).get("long_name", "")
        if uk_ire_only and country not in UK_IRE:
            continue
        for race in meeting.get("races") or []:
            if not race.get("hidden"):
                yield ms, race


def race_url(rs):
    return (f"{sl_results.BASE}/racing/racecards/{rs['date']}/{slugify(rs['course_name'])}"
            f"/racecard/{rs['race_summary_reference']['id']}/{slugify(rs['name'])}")


def fetch_race(client, ms, rs):
    path = race_url(rs)[len(sl_results.BASE):]
    race = client.page_props(path).get("race")
    if not race:
        log.warning("no race data for %s", path)
        return None
    race["_meeting"] = ms
    # The index page carries the meeting going and occasionally fields the card page lacks.
    for k, v in rs.items():
        race["race_summary"].setdefault(k, v)
    return race


def strip_html(text):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text or "")).split())


def verdict_parts(rs):
    """(verdict text, [1st, 2nd, 3rd] selections) from the Spotlight verdict."""
    raw = rs.get("verdict") or ""
    picks = [strip_html(p) for p in re.findall(r"<li>(.*?)</li>", raw, re.S)]
    text = rs.get("verdict_text") or strip_html(re.sub(r"<ol>.*?</ol>", "", raw, flags=re.S))
    return text or "", picks


def norm_name(name):
    """'GREEN STAR (IRE)' / 'Green Star' -> 'green star'."""
    return re.sub(r"\s*\([A-Z]{2,3}\)\s*$", "", name or "", flags=re.I).strip().lower()


def forecast_odds(text):
    """'King High (2/1), Undiscovered (4/1)' -> {'king high': '2/1', ...}."""
    return {norm_name(h): o for h, o in re.findall(r"([^,(]+?)\s*\(([^)]+)\)", text or "")}


def race_row(race):
    rs, ms = race["race_summary"], race.get("_meeting", {})
    country = ms.get("course", {}).get("country", {}).get("long_name")
    surface = (rs.get("course_surface") or {}).get("surface")
    prizes = sorted((race.get("prizes") or {}).get("prize") or [], key=lambda p: p.get("position") or 0)
    verdict, picks = verdict_parts(rs)
    lyw = (race.get("last_years_winners") or [{}])[0]
    rides = race.get("rides") or []
    fund = sum(float(money(p.get("prize")) or 0) for p in prizes)
    return {
        "race_id": rs["race_summary_reference"]["id"],
        "date": rs.get("date"),
        "course": rs.get("course_name"),
        "country": country,
        "time": uk_time(rs.get("date"), rs.get("time")),
        "race_name": rs.get("name"),
        "race_class": rs.get("race_class"),
        "age": rs.get("age"),
        "distance": rs.get("distance"),
        "going": rs.get("going") or ms.get("going"),
        "surface": surface,
        "handicap": rs.get("has_handicap"),
        "runners": sum(1 for r in rides if r.get("ride_status") not in NON_RUNNER) if rides
                   else rs.get("ride_count"),
        "declared": len(rides) or rs.get("ride_count"),
        "places": race.get("number_of_placed_rides"),
        "stage": rs.get("race_stage"),
        "prize_fund": f"{fund:g}" if fund else "",
        "winner_prize": money(prizes[0].get("prize")) if prizes else "",
        "prizes": " / ".join(money(p.get("prize")) for p in prizes),
        "betting_forecast": race.get("betting_forecast"),
        "book_percentage": race.get("on_course_book_percentage"),
        "verdict": verdict,
        "verdict_1st": picks[0] if len(picks) > 0 else "",
        "verdict_2nd": picks[1] if len(picks) > 1 else "",
        "verdict_3rd": picks[2] if len(picks) > 2 else "",
        "last_year_winner": (lyw.get("horse") or {}).get("name"),
        "last_year_trainer": (lyw.get("trainer") or {}).get("name"),
        "last_year_jockey": (lyw.get("jockey") or {}).get("name"),
        "last_year_weight": lyw.get("weight"),
        "last_year_odds": lyw.get("odds"),
        "url": race_url(rs),
        "rp_course": sl_results.rp_course(rs.get("course_name"), surface, country),
        "rp_type": sl_results.rp_type(rs.get("name")),
        "rp_class": f"Class {rs['race_class']}" if rs.get("race_class") else "",
        "rp_pattern": sl_results.rp_pattern(rs.get("name")),
        "rp_rating_band": (re.search(r"\((\d+-\d+)\)", rs.get("name") or "") or [None, ""])[1],
        "rp_age_band": sl_results.rp_age_band(rs.get("age")),
        "rp_sex_rest": sl_results.rp_sex_rest(rs.get("name")),
        "rp_dist": sl_results.rp_dist(rs.get("distance")),
        "rp_going": sl_results.rp_going(rs.get("going") or ms.get("going")),
    }


def best_odds(ride):
    """(fractional, decimal, bookmaker) of the best bookmaker price, or blanks."""
    offers = [o for o in ride.get("bookmakerOdds") or [] if o.get("decimalOdds")]
    if not offers:
        return "", "", ""
    best = next((o for o in offers if o.get("bestOdds")), None) or max(offers, key=lambda o: o["decimalOdds"])
    return best.get("fractionalOdds"), best.get("decimalOdds"), best.get("bookmakerName")


def last_runs(horse):
    out = {}
    runs = sorted(horse.get("previous_results") or [], key=lambda r: r.get("date") or "", reverse=True)
    for i, run in enumerate(runs[:LAST_RUNS], 1):
        reason = (run.get("casualty") or {}).get("reason", "")
        pos = run.get("position") or sl_results.CASUALTY_CODES.get(reason, reason)
        out.update({
            f"last{i}_date": run.get("date"),
            f"last{i}_course": run.get("course_name"),
            f"last{i}_pos": pos,
            f"last{i}_ran": run.get("runner_count"),
            f"last{i}_dist": run.get("distance"),
            f"last{i}_going": run.get("going"),
            f"last{i}_class": run.get("race_class"),
            f"last{i}_weight": run.get("weight"),
            f"last{i}_odds": run.get("odds"),
            f"last{i}_comment": run.get("ride_description"),
        })
    return out


def runner_rows(race):
    rs = race["race_summary"]
    forecast = forecast_odds(race.get("betting_forecast"))
    _, picks = verdict_parts(rs)
    picks = {norm_name(p): i for i, p in enumerate(picks, 1)}
    rides = sorted(race.get("rides") or [], key=lambda r: r.get("cloth_number") or 999)
    for ride in rides:
        horse = ride.get("horse") or {}
        headgear = ride.get("headgear") or []
        life = (ride.get("horse_lifetime_stats") or [{}])[0]
        bo, bd, bb = best_odds(ride)
        name = horse.get("name")
        yield {
            "race_id": rs["race_summary_reference"]["id"],
            "date": rs.get("date"),
            "course": rs.get("course_name"),
            "time": uk_time(rs.get("date"), rs.get("time")),
            "race_name": rs.get("name"),
            "cloth": ride.get("cloth_number"),
            "draw": ride.get("draw_number"),
            "horse": name,
            "horse_id": (horse.get("horse_reference") or {}).get("id"),
            "horse_age": horse.get("age"),
            "sex": (horse.get("sex") or {}).get("type"),
            "colour": ride.get("horseColour"),
            "foaled": horse.get("foaled"),
            "form": (horse.get("formsummary") or {}).get("display_text"),
            "last_ran_days": horse.get("last_ran_days"),
            "weight": ride.get("handicap"),
            "headgear": ",".join(h.get("name", h.get("symbol", "")) for h in headgear),
            "headgear_first_time": ",".join(h.get("symbol", "") for h in headgear if h.get("count") == 1),
            "official_rating": ride.get("official_rating"),
            "jockey": (ride.get("jockey") or {}).get("name"),
            "jockey_claim": ride.get("jockey_claim"),
            "trainer": (ride.get("trainer") or {}).get("name"),
            "owner": (ride.get("owner") or {}).get("name"),
            "status": ride.get("ride_status"),
            "odds": (ride.get("betting") or {}).get("current_odds"),
            "forecast_odds": forecast.get(norm_name(name), ""),
            "best_odds": bo,
            "best_odds_decimal": bd,
            "best_odds_bookie": bb,
            "timeform_stars": ride.get("timeform_stars"),
            "sl_rating": ride.get("rating123"),
            "verdict_pick": picks.get(norm_name(name), ""),
            "stats": " ".join(STAT_CODES.get(s.get("type"), s.get("type", ""))
                              for s in ride.get("race_history_stats") or []),
            "runs": life.get("run_count"),
            "wins": life.get("win_count"),
            "placed": life.get("place_count"),
            "commentary": (ride.get("commentary") or "").strip(),
            "rp_sex": ((horse.get("sex") or {}).get("type") or "").upper(),
            "rp_hg": "".join(h.get("symbol", "") for h in headgear),
            **last_runs(horse),
        }


def parse_args(argv):
    p = argparse.ArgumentParser(description="Scrape racecards from sportinglife.com")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--tomorrow", action="store_true", help="tomorrow's cards (default: today)")
    g.add_argument("--date", type=date.fromisoformat, help="a date up to about 6 days ahead")
    p.add_argument("--uk-ire", action="store_true", help="only UK and Irish meetings")
    p.add_argument("--out", type=Path, default=Path("data"), help="output directory (default: data)")
    p.add_argument("--delay", type=float, default=1.0, help="seconds between requests (default: 1.0)")
    p.add_argument("--no-breeding", dest="breeding", action="store_false",
                   help="skip fetching sire/dam/damsire (one extra request per new horse)")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)
    a.day = a.date or date.today() + timedelta(days=1 if a.tomorrow else 0)
    return a


def scrape(day, out, uk_ire=False, delay=1.0, breeding=True, on_race=None, on_progress=None, stop=None):
    """Scrape one day's racecards into cards_races_/cards_runners_ CSVs; return their paths.

    on_race(race_row, runner_rows) is called per race, on_progress(index, total) before each
    race, and scraping stops early if stop (a threading.Event) gets set.
    """
    client = Client(delay=delay)
    raw_dir = out / "raw"
    out.mkdir(parents=True, exist_ok=True)
    races_path = out / f"cards_races_{day}.csv"
    runners_path = out / f"cards_runners_{day}.csv"

    summaries = list(day_races(client, day, uk_ire))
    log.info("%s: %d races", day, len(summaries))
    n_races = n_runners = 0
    with open(races_path, "w", newline="") as fr, open(runners_path, "w", newline="") as fu:
        races_w = csv.DictWriter(fr, RACE_FIELDS + RP_RACE_FIELDS, extrasaction="ignore")
        runners_w = csv.DictWriter(fu, RUNNER_FIELDS + RP_RUNNER_FIELDS, extrasaction="ignore")
        races_w.writeheader()
        runners_w.writeheader()
        for i, (ms, rs) in enumerate(summaries):
            if stop and stop.is_set():
                log.info("stopped")
                break
            if on_progress:
                on_progress(i, len(summaries))
            try:
                race = fetch_race(client, ms, rs)
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
                        u.update(sl_results.fetch_breeding(client, raw_dir, u["horse_id"]))
                    except RuntimeError as e:
                        log.error("breeding for %s: %s", u["horse"], e)
            log.info("%s %s %s (%d)", rrow["time"], rrow["course"], rrow["race_name"], len(urows))
            races_w.writerow(rrow)
            runners_w.writerows(urows)
            fr.flush()
            fu.flush()
            n_races += 1
            n_runners += len(urows)
            if on_race:
                on_race(rrow, urows)

    log.info("wrote %d races -> %s", n_races, races_path)
    log.info("wrote %d runners -> %s", n_runners, runners_path)
    return races_path, runners_path


def main(argv=None):
    a = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    scrape(a.day, a.out, uk_ire=a.uk_ire, delay=a.delay, breeding=a.breeding)


if __name__ == "__main__":
    sys.exit(main())
