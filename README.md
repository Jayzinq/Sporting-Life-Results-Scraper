# Sporting Life results scraper

Scrapes UK/IRE (and international) horse racing results from sportinglife.com.
History goes back to about 2004–05.

## Setup

Copy these files into one folder:

- `gui.py`, `sl_results.py`, `export_dialog.py`, `export_config.json`
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

To activate the env outside this folder, run `pyenv activate atrs`. This needs
`eval "$(pyenv virtualenv-init -)"` in `~/.bashrc`; an alias such as `alias atrs='pyenv activate atrs'`
makes it shorter. Leave it with `pyenv deactivate`.

## GUI

`gui.py` has six tabs: **Scrape results** (below), **Join CSV files** (combine several CSVs,
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

