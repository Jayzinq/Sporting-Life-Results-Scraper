#!/usr/bin/env python
"""Tkinter front end for sl_racecards: scrape today's or tomorrow's cards, browse them, export CSV."""

import logging
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from datetime import date, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import sl_racecards as cards
from export_dialog import ExportDialog

HERE = Path(__file__).resolve().parent
DAYS_AHEAD = 6  # the site lists cards about this far ahead
ALL = "All courses"

RACE_COLS = [("time", "Time", 50), ("course", "Course", 110), ("race_name", "Race", 320),
             ("race_class", "Cls", 35), ("distance", "Dist", 70), ("going", "Going", 120),
             ("runners", "Rnrs", 45), ("winner_prize", "1st prize", 70),
             ("verdict_1st", "Spotlight pick", 140), ("favourite", "Fcst fav", 140)]
RUNNER_COLS = [("cloth", "No", 35), ("draw", "Dr", 35), ("horse", "Horse", 150),
               ("horse_age", "Age", 35), ("weight", "Wgt", 45), ("official_rating", "OR", 40),
               ("rp_hg", "Hg", 35), ("jockey", "Jockey", 130), ("jockey_claim", "Clm", 35),
               ("trainer", "Trainer", 130), ("form", "Form", 70), ("last_ran_days", "Days", 40),
               ("odds", "Odds", 50), ("best_odds", "Best", 50), ("timeform_stars", "TF★", 35),
               ("verdict_pick", "Pick", 35), ("stats", "C/D", 50), ("status", "Status", 80)]

# Runner rows carry their race's details too, as in the results export.
DATASETS = {
    "Runners": cards.RUNNER_FIELDS + cards.RP_RUNNER_FIELDS
               + [f for f in cards.RACE_FIELDS if f not in cards.RUNNER_FIELDS]
               + cards.RP_RACE_FIELDS,
    "Races": cards.RACE_FIELDS + cards.RP_RACE_FIELDS,
}
DEFAULT_INCLUDED = {
    "Runners": set(cards.RUNNER_FIELDS) - set(cards.LAST_RUN_FIELDS)
               | {"distance", "going", "race_class", "prize_fund"},
    "Races": set(cards.RACE_FIELDS),
}


class QueueHandler(logging.Handler):
    def __init__(self, q):
        super().__init__()
        self.q = q

    def emit(self, record):
        self.q.put(("log", self.format(record)))


class RacecardsFrame(ttk.Frame):
    def __init__(self, master):
        super().__init__(master)

        self.q = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self.cards = []  # (race row, runner rows) in scrape order
        self.by_iid = {}  # race treeview iid -> (race row, runner rows)
        self.day = None

        handler = QueueHandler(self.q)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
        cards.log.addHandler(handler)
        cards.log.setLevel(logging.INFO)

        self._build_controls()
        self._build_results()
        self._build_log()
        self.after(100, self._poll)

    # --- layout -----------------------------------------------------------

    def _build_controls(self):
        f = ttk.Frame(self, padding=8)
        f.pack(fill="x")

        today = date.today()
        self.days = {}
        for n in range(DAYS_AHEAD + 1):
            d = today + timedelta(days=n)
            label = {0: "Today", 1: "Tomorrow"}.get(n, d.strftime("%A"))
            self.days[f"{label} ({d.strftime('%a %d %b')})"] = d
        ttk.Label(f, text="Day").grid(row=0, column=0, sticky="w")
        self.day_var = tk.StringVar(value=next(iter(self.days)))
        ttk.Combobox(f, textvariable=self.day_var, values=list(self.days), state="readonly",
                     width=24).grid(row=0, column=1, sticky="w", padx=(4, 12))

        self.uk_ire_var = tk.BooleanVar(value=True)
        self.breeding_var = tk.BooleanVar(value=False)
        self.delay_var = tk.DoubleVar(value=1.0)
        self.out_var = tk.StringVar(value=str(HERE / "data"))
        ttk.Checkbutton(f, text="UK & Ireland only", variable=self.uk_ire_var
                        ).grid(row=0, column=2, sticky="w")
        ttk.Checkbutton(f, text="Get sire/dam (slower)", variable=self.breeding_var
                        ).grid(row=0, column=3, sticky="w", padx=12)
        dl = ttk.Frame(f)
        dl.grid(row=0, column=4, sticky="w")
        ttk.Label(dl, text="Delay (s)").pack(side="left")
        ttk.Spinbox(dl, from_=0.5, to=10, increment=0.5, textvariable=self.delay_var, width=5
                    ).pack(side="left", padx=4)

        ttk.Label(f, text="Output").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(f, textvariable=self.out_var).grid(row=1, column=1, columnspan=4, sticky="ew",
                                                     padx=(4, 4), pady=(6, 0))
        ttk.Button(f, text="Browse…", command=self._browse).grid(row=1, column=5, pady=(6, 0))
        ttk.Button(f, text="Open folder", command=self._open_folder).grid(row=1, column=6, pady=(6, 0))
        f.columnconfigure(4, weight=1)

        bar = ttk.Frame(self, padding=(8, 0, 8, 4))
        bar.pack(fill="x")
        self.start_btn = ttk.Button(bar, text="Scrape", command=self._start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(bar, text="Stop", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=4)
        self.export_btn = ttk.Button(bar, text="Export CSV…", command=self._export, state="disabled")
        self.export_btn.pack(side="left")
        self.progress = ttk.Progressbar(bar, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=8)
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(bar, textvariable=self.status_var, width=30).pack(side="left")

        flt = ttk.Frame(self, padding=(8, 0, 8, 6))
        flt.pack(fill="x")
        ttk.Label(flt, text="Show").pack(side="left")
        self.course_var = tk.StringVar(value=ALL)
        self.course_cb = ttk.Combobox(flt, textvariable=self.course_var, values=[ALL],
                                      state="readonly", width=22)
        self.course_cb.pack(side="left", padx=6)
        self.course_cb.bind("<<ComboboxSelected>>", lambda e: self._refill_races())
        ttk.Label(flt, text="Export").pack(side="left", padx=(16, 4))
        self.scope_var = tk.StringVar(value="shown")
        ttk.Radiobutton(flt, text="all races shown", value="shown", variable=self.scope_var
                        ).pack(side="left")
        ttk.Radiobutton(flt, text="selected races only", value="selected", variable=self.scope_var
                        ).pack(side="left", padx=6)
        ttk.Label(flt, text="(Ctrl/Shift-click to select several races)", foreground="grey"
                  ).pack(side="left", padx=6)

    def _tree(self, parent, cols, stretch, selectmode="browse"):
        frame = ttk.Frame(parent)
        tree = ttk.Treeview(frame, columns=[c for c, _, _ in cols], show="headings",
                            selectmode=selectmode)
        for key, title, width in cols:
            tree.heading(key, text=title, command=lambda k=key, t=tree: self._sort(t, k, False))
            tree.column(key, width=width, stretch=key in stretch)
        ys = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        xs = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        tree.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        return frame, tree

    def _build_results(self):
        pane = ttk.PanedWindow(self, orient="vertical")
        pane.pack(fill="both", expand=True, padx=8)
        rf, self.race_tree = self._tree(pane, RACE_COLS, ("race_name",), "extended")
        lower = ttk.PanedWindow(pane, orient="horizontal")
        uf, self.runner_tree = self._tree(lower, RUNNER_COLS, ("horse",))
        df = ttk.Frame(lower)
        self.detail = tk.Text(df, wrap="word", state="disabled", width=50)
        ys = ttk.Scrollbar(df, orient="vertical", command=self.detail.yview)
        self.detail.configure(yscrollcommand=ys.set)
        self.detail.pack(side="left", fill="both", expand=True)
        ys.pack(side="left", fill="y")
        self.detail.tag_configure("h", font=("TkDefaultFont", 10, "bold"))
        lower.add(uf, weight=3)
        lower.add(df, weight=2)
        pane.add(rf, weight=2)
        pane.add(lower, weight=3)
        self.race_tree.bind("<<TreeviewSelect>>", self._show_race)
        self.runner_tree.bind("<<TreeviewSelect>>", self._show_runner)

    def _build_log(self):
        f = ttk.Frame(self, padding=8)
        f.pack(fill="x")
        self.log_text = tk.Text(f, height=5, state="disabled", wrap="none")
        ys = ttk.Scrollbar(f, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=ys.set)
        self.log_text.pack(side="left", fill="x", expand=True)
        ys.pack(side="left", fill="y")

    # --- actions ----------------------------------------------------------

    def _browse(self):
        d = filedialog.askdirectory(initialdir=self.out_var.get())
        if d:
            self.out_var.set(d)

    def _open_folder(self):
        path = Path(self.out_var.get())
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform.startswith("win"):
            os.startfile(path)
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])

    def _start(self):
        try:
            delay = max(float(self.delay_var.get()), 0.5)  # stay polite to the site
        except (ValueError, tk.TclError):
            messagebox.showerror("Invalid input", "Delay must be a number.")
            return
        self.day = self.days[self.day_var.get()]

        self.cards.clear()
        self.by_iid.clear()
        self.race_tree.delete(*self.race_tree.get_children())
        self.runner_tree.delete(*self.runner_tree.get_children())
        self._set_detail([])
        self.course_var.set(ALL)
        self.course_cb.configure(values=[ALL])
        self.export_btn.configure(state="disabled")
        self.stop_event.clear()
        self.progress.configure(value=0, maximum=1)
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status_var.set("Fetching meetings…")

        kwargs = dict(out=Path(self.out_var.get()), uk_ire=self.uk_ire_var.get(), delay=delay,
                      breeding=self.breeding_var.get(), stop=self.stop_event,
                      on_race=lambda r, u: self.q.put(("race", (r, u))),
                      on_progress=lambda i, n: self.q.put(("progress", (i, n))))
        self.worker = threading.Thread(target=self._run, args=(self.day,), kwargs=kwargs, daemon=True)
        self.worker.start()

    def _run(self, day, **kwargs):
        try:
            self.q.put(("done", cards.scrape(day, **kwargs)))
        except Exception as e:  # surface anything unexpected in the GUI
            self.q.put(("error", str(e)))

    def _stop(self):
        self.stop_event.set()
        self.stop_btn.configure(state="disabled")
        self.status_var.set("Stopping…")

    def _export(self):
        if self.scope_var.get() == "selected" and not self.race_tree.selection():
            messagebox.showinfo("Export", "Select one or more races first, or export all races shown.")
            return
        ExportDialog(self.winfo_toplevel(), self._rows_for, self.out_var.get(), datasets=DATASETS,
                     default_included=DEFAULT_INCLUDED, config_key="racecards",
                     file_prefix=f"cards_{self.day}_")

    def _rows_for(self, dataset):
        sel = set(self.race_tree.selection())
        order = [i for i in self.race_tree.get_children()  # export in the order currently shown
                 if self.scope_var.get() == "shown" or i in sel]
        if dataset == "Races":
            return [self.by_iid[i][0] for i in order]
        return [{**self.by_iid[i][0], **r} for i in order for r in self.by_iid[i][1]]

    def _refill_races(self):
        self.race_tree.delete(*self.race_tree.get_children())
        self.runner_tree.delete(*self.runner_tree.get_children())
        self._set_detail([])
        self.by_iid.clear()
        for race, runners in self.cards:
            self._insert_race(race, runners)

    def _insert_race(self, race, runners):
        course = self.course_var.get()
        if course != ALL and race["course"] != course:
            return
        fav = race.get("betting_forecast") or ""
        values = {**race, "favourite": fav.split(",")[0].strip()}
        iid = self.race_tree.insert("", "end", values=[_fmt(values.get(c)) for c, _, _ in RACE_COLS])
        self.by_iid[iid] = (race, runners)

    def _set_detail(self, parts):
        """parts: list of (text, tag) pairs."""
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        for text, tag in parts:
            self.detail.insert("end", text, tag)
        self.detail.configure(state="disabled")

    def _show_race(self, _event=None):
        self.runner_tree.delete(*self.runner_tree.get_children())
        sel = self.race_tree.selection()
        if not sel:
            self._set_detail([])
            return
        race, runners = self.by_iid[sel[-1]]
        for i, row in enumerate(runners):
            self.runner_tree.insert("", "end", iid=str(i),
                                    values=[_fmt(row.get(c)) for c, _, _ in RUNNER_COLS])
        parts = [(f"{race['time']} {race['course']} – {race['race_name']}\n", "h"),
                 (f"Class {race['race_class'] or '-'} · {race['age'] or ''} · {race['distance']} · "
                  f"{race['going']} · {race['runners']} runners ({race['places'] or '?'} places)\n", None)]
        if race["prizes"]:
            parts.append((f"Prizes: {race['prizes']}\n", None))
        if race["betting_forecast"]:
            parts += [("\nBetting forecast\n", "h"), (race["betting_forecast"] + "\n", None)]
        if race["verdict"]:
            picks = ", ".join(p for p in (race["verdict_1st"], race["verdict_2nd"], race["verdict_3rd"]) if p)
            parts += [("\nSpotlight verdict\n", "h"), (race["verdict"] + "\n", None),
                      (f"Picks: {picks}\n" if picks else "", None)]
        if race["last_year_winner"]:
            parts += [("\nLast year\n", "h"),
                      (f"{race['last_year_winner']} {race['last_year_odds'] or ''} "
                       f"({race['last_year_trainer']} / {race['last_year_jockey']}, "
                       f"{race['last_year_weight']})\n", None)]
        parts.append(("\nClick a runner for its comment and recent form.", None))
        self._set_detail(parts)

    def _show_runner(self, _event=None):
        sel_race, sel = self.race_tree.selection(), self.runner_tree.selection()
        if not sel_race or not sel:
            return
        r = self.by_iid[sel_race[-1]][1][int(sel[0])]
        parts = [(f"{r['cloth']}. {r['horse']}\n", "h"),
                 (f"{r['horse_age']}yo {r['colour'] or ''} {r['sex'] or ''} · foaled {r['foaled'] or '?'}"
                  + (f" · {r['sire']} – {r['dam']} ({r['damsire']})" if r.get("sire") else "") + "\n", None),
                 (f"Trainer {r['trainer']} · Jockey {r['jockey']}"
                  + (f" ({r['jockey_claim']})" if r["jockey_claim"] else "") + f" · Owner {r['owner']}\n", None),
                 (f"Odds {r['odds'] or '-'} · Best {r['best_odds'] or '-'} ({r['best_odds_bookie'] or '-'})"
                  f" · Forecast {r['forecast_odds'] or '-'}\n", None),
                 (f"Career {r['runs'] or 0} runs, {r['wins'] or 0} wins, {r['placed'] or 0} placed"
                  + (f" · {r['stats']}" if r["stats"] else "") + "\n", None)]
        if r["headgear"]:
            parts.append((f"Headgear: {r['headgear']}"
                          + (f" (first time: {r['headgear_first_time']})" if r["headgear_first_time"] else "")
                          + "\n", None))
        if r["commentary"]:
            parts += [("\nComment\n", "h"), (r["commentary"] + "\n", None)]
        runs = [i for i in range(1, cards.LAST_RUNS + 1) if r.get(f"last{i}_date")]
        if runs:
            parts.append(("\nRecent runs\n", "h"))
        for i in runs:
            g = lambda k: r.get(f"last{i}_{k}") or ""
            parts.append((f"{g('date')} {g('course')}: {g('pos')}/{g('ran')} · {g('dist')} · {g('going')}"
                          f" · Cls {g('class') or '-'} · {g('weight')} · {g('odds')}\n", None))
            if g("comment"):
                parts.append((f"    {g('comment')}\n", None))
        self._set_detail(parts)

    def _sort(self, tree, col, reverse):
        def key(iid):
            v = tree.set(iid, col)
            try:
                return (0, float(v))
            except ValueError:
                return (1, v.lower())
        items = sorted(tree.get_children(""), key=key, reverse=reverse)
        for i, iid in enumerate(items):
            tree.move(iid, "", i)
        tree.heading(col, command=lambda: self._sort(tree, col, not reverse))

    # --- worker -> UI -----------------------------------------------------

    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                getattr(self, f"_on_{kind}")(payload)
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _on_log(self, msg):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _on_progress(self, payload):
        i, n = payload
        self.progress.configure(value=i, maximum=n)
        self.status_var.set(f"Race {i + 1}/{n}")

    def _on_race(self, payload):
        race, runners = payload
        self.cards.append(payload)
        courses = sorted({r["course"] for r, _ in self.cards})
        self.course_cb.configure(values=[ALL] + courses)
        self._insert_race(race, runners)

    def _finish(self, status):
        self.progress.configure(value=self.progress["maximum"])
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.export_btn.configure(state="normal" if self.cards else "disabled")
        self.status_var.set(status)

    def _on_done(self, paths):
        n = len(self.cards)
        self._finish(("Stopped" if self.stop_event.is_set() else "Done") + f" – {n} races")
        if n:
            self._on_log(f"Saved {paths[0].name} and {paths[1].name}")
        else:
            self._on_log("No races found. Sporting Life may not have published cards for that day yet.")

    def _on_error(self, msg):
        self._finish("Error")
        messagebox.showerror("Scrape failed", msg)


def _fmt(v):
    return "" if v is None else v


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Sporting Life Racecards")
        self.geometry("1350x850")
        self.minsize(900, 550)
        RacecardsFrame(self).pack(fill="both", expand=True)


if __name__ == "__main__":
    App().mainloop()
