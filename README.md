# Sporting Life results scraper

Scrapes UK/IRE (and international) horse racing results from sportinglife.com.
History goes back to about 2004–05. Also scrapes racecards for today, tomorrow and a few days ahead.

## Setup

Copy these files into one folder:

- `gui.py`, `sl_results.py`, `export_dialog.py`, `export_config.json`
- `racecards_gui.py`, `sl_racecards.py`
- `join_csv_gui.py`, `join_csv_files.py`, `csvtotable_gui.py`, `raceform_tk.py`
- `requirements.txt`
- optional: `.schema` (a hidden file; the CSV → SQLite tab picks it up automatically) and `data/`
  (earlier scrapes and the download cache)

Python needs Tk. On Ubuntu/Debian, install `tk-dev` *before* building a pyenv Python (or use
`sudo apt install python3-tk` with the system Python). Check with `python -c "import tkinter"`.

With pyenv and pyenv-virtualenv:

    sudo apt install tk-dev                 # only if building a new pyenv Python
    pyenv install 3.14.3                    # skip if already installed
    pyenv virtualenv 3.14.3 atrs
    cd ~/atrs
    pyenv local atrs                        # writes .python-version
    pip install -r requirements.txt

Without pyenv, a plain venv works too:

    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt

## Run

    cd ~/atrs                 # pyenv auto-selects the `atrs` virtualenv
    python gui.py             # GUI
    python raceform_tk.py [file.csv | file.db]                         # CSV/SQLite browser on its own
    python sl_results.py --from 2026-10-01 --to 2026-10-07 --uk-ire   # CLI
    python racecards_gui.py                                            # Racecards on their own
    python sl_racecards.py --tomorrow --uk-ire                         # racecards CLI (or --date)

To activate the env outside this folder, run `pyenv activate atrs`. This needs
`eval "$(pyenv virtualenv-init -)"` in `~/.bashrc`; an alias such as `alias atrs='pyenv activate atrs'`
makes it shorter. Leave it with `pyenv deactivate`.

## GUI

`gui.py` has seven tabs: **Scrape results** (below), **Racecards** (see further down), **Join CSV files** (combine several CSVs,
keeping the first file's header; warns if headers differ), **CSV to table** (turn a CSV into a
styled HTML table with csvtotable), and the three raceform tabs from `raceform_tk.py`:
**CSV Viewer** (page through very large CSVs), **SQLite Viewer** (browse tables or run SQL) and
**CSV → SQLite** (build a database from a CSV using `.schema`). `join_csv_gui.py`,
`csvtotable_gui.py` and `raceform_tk.py` still run on their own.

1. Pick a date range (or use Today / Yesterday / Last 7 / Last 30 days) and click **Scrape**.
2. Click a race to see its full result underneath. Click a column heading to sort.
3. Click **Export CSV…** and choose:
   - **Runners**: one row per horse (race details can be included as columns), or **Races**: one row per race.
   - Tick or untick columns, reorder them (Top/Up/Down/Bottom), and rename headers.
   - Blank columns (5 by default; use **Add blank** / **Remove** to change how many) export as empty
     cells, so the CSV can line up with other spreadsheets. Move them where you need gaps; their
     headers can be left empty or named.
   - Your column setup is saved in `export_config.json` and reused next time.

Each scrape also writes full `races_*.csv` / `runners_*.csv` dumps to the output folder.
Raw race JSON is cached in `<output>/raw/`, so rescraping the same dates is quick.
Requests are spaced at least 0.5 s apart (1 s by default) to go easy on the site.

## Racecards

1. Pick a day (Today, Tomorrow or up to 6 days ahead) and click **Scrape**. Untick **UK & Ireland only**
   for overseas meetings; **Get sire/dam** adds breeding as for results (cached in `<output>/raw/horses/`).
2. Click a race to see its runners, plus the betting forecast, prizes, Spotlight verdict and last year's
   winner on the right. Click a runner for its comment, career record and last 3 runs.
3. Use **Show** to list one course only. **Export CSV…** opens the same column chooser as results
   (its setup is saved separately), for **all races shown** or **selected races only**
   (Ctrl/Shift-click races to pick several).

Runner fields include cloth, draw, age, sex, colour, form, days since last run, weight, headgear (and
first-time headgear), OR, jockey and claim, trainer, owner, the site's odds, forecast odds, best
bookmaker price, Timeform stars, Sporting Life's 1-2-3 rating, Spotlight pick, C/D/BF stats, career
runs/wins/places, the Spotlight comment and `last1_*`..`last3_*` details of recent runs (untick by
default). Each scrape also writes `cards_races_<date>.csv` / `cards_runners_<date>.csv` to the output
folder. Cards change up to the off (non-runners, prices), so they are not cached: rescrape for
fresh data.

Sporting Life sometimes sends the Tomorrow and later dates back to today's page, for example just after
midnight. The scraper then finds no races for that day and says so; try again later.

## Matching a Racing Post (rpscrape) sheet

In the export dialog, **RP layout** sets up the exact rpscrape column order and headers
(`date,course,race_id,off,race_name,type,...,owner,comment`) and ticks **Exclude non-runners**.
Fields starting with `rp_` are Racing Post-style versions of the data: course with `(AW)`/`(IRE)`,
distance rounded to the nearest half furlong (`1m1f`), going in title case, `PU`/`F`/`UR` positions,
beaten lengths as numbers (`nk` = 0.3) and prize money by finishing position.

`sire`, `dam` and `damsire` come from each horse's profile page (its Breeding section). That's one
extra request per horse the first time it's seen, so the first scrape of a date range is much slower;
profiles are cached in `<output>/raw/horses/` for good. Untick **Get sire/dam** (or pass
`--no-breeding`) to skip it.

Not available from Sporting Life (left as named blank columns): `rpr`, `ts`.
Other differences to bear in mind when joining:
- `race_id` is Sporting Life's id, not Racing Post's, so it won't match.
- Horse names have no country suffix: RP has `Coppull (GB)`, Sporting Life has `Coppull`.
- Trainers are often initials (`I Jardine` rather than `Ian Jardine`).
- `time` is filled in for the winner only.
- `type`, `pattern`, `sex_rest` and `rating_band` are worked out from the race name.

Join on **date + off + horse** (with the ` (XX)` suffix stripped from the RP horse name);
`course` usually matches too.
All times are UK local time (Sporting Life stores UTC; the scraper converts).
