#!/usr/bin/env python
"""Tkinter front end for sl_results: pick a date range, scrape, browse the results."""

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

import sl_results
from csvtotable_gui import CsvToTableFrame
from export_dialog import ExportDialog
from join_csv_gui import JoinCsvApp
from raceform_tk import RaceformTabs
from racecards_gui import RacecardsFrame

HERE = Path(__file__).resolve().parent

RACE_COLS = [("date", "Date", 85), ("time", "Time", 50), ("course", "Course", 110),
             ("race_name", "Race", 300), ("distance", "Dist", 70), ("going", "Going", 100),
             ("runners", "Rnrs", 45), ("winner", "Winner", 150), ("sp", "SP", 55)]
RUNNER_COLS = [("position", "Pos", 40), ("beaten", "Btn", 45), ("horse", "Horse", 150),
               ("horse_age", "Age", 40), ("weight", "Wgt", 50), ("official_rating", "OR", 40),
               ("jockey", "Jockey", 140), ("trainer", "Trainer", 140), ("sp", "SP", 55),
               ("status", "Status", 85), ("comment", "Comment", 400)]


class QueueHandler(logging.Handler):
    def __init__(self, q):
        super().__init__()
        self.q = q

    def emit(self, record):
        self.q.put(("log", self.format(record)))


class ResultsFrame(ttk.Frame):
    def __init__(self, master):
        super().__init__(master)

        self.q = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self.runners = {}  # race treeview iid -> runner rows
        self.races = {}  # race treeview iid -> race row

        handler = QueueHandler(self.q)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
        sl_results.log.addHandler(handler)
        sl_results.log.setLevel(logging.INFO)

        self._build_controls()
        self._build_results()
        self._build_log()
        self.after(100, self._poll)

    # --- layout -----------------------------------------------------------

    def _build_controls(self):
        f = ttk.Frame(self, padding=8)
        f.pack(fill="x")

        today = date.today()
        self.start_var = tk.StringVar(value=today.isoformat())
        self.end_var = tk.StringVar(value=today.isoformat())
        ttk.Label(f, text="From").grid(row=0, column=0, sticky="w")
        ttk.Entry(f, textvariable=self.start_var, width=12).grid(row=0, column=1, padx=(4, 12))
        ttk.Label(f, text="To").grid(row=0, column=2, sticky="w")
        ttk.Entry(f, textvariable=self.end_var, width=12).grid(row=0, column=3, padx=(4, 12))

        quick = ttk.Frame(f)
        quick.grid(row=0, column=4, columnspan=4, sticky="w")
        for label, back, span in [("Today", 0, 1), ("Yesterday", 1, 1), ("Last 7 days", 0, 7),
                                  ("Last 30 days", 0, 30)]:
            ttk.Button(quick, text=label, command=lambda b=back, s=span: self._set_range(b, s)
                       ).pack(side="left", padx=2)

        self.uk_ire_var = tk.BooleanVar(value=True)
        self.refresh_var = tk.BooleanVar(value=False)
        self.breeding_var = tk.BooleanVar(value=True)
        self.intl_pattern_var = tk.BooleanVar(value=False)
        self.delay_var = tk.DoubleVar(value=1.0)
        self.out_var = tk.StringVar(value=str(HERE / "data"))

        ttk.Checkbutton(f, text="UK & Ireland only", variable=self.uk_ire_var
                        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Checkbutton(f, text="Ignore cache", variable=self.refresh_var
                        ).grid(row=1, column=2, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Checkbutton(f, text="Get sire/dam (slower)", variable=self.breeding_var
                        ).grid(row=1, column=4, sticky="w", pady=(6, 0))
        dl = ttk.Frame(f)
        dl.grid(row=1, column=5, sticky="w", pady=(6, 0))
        ttk.Label(dl, text="Delay (s)").pack(side="left")
        ttk.Spinbox(dl, from_=0.5, to=10, increment=0.5, textvariable=self.delay_var, width=5
                    ).pack(side="left", padx=4)
        ttk.Checkbutton(f, text="Pattern/Listed only outside UK & IRE", variable=self.intl_pattern_var
                        ).grid(row=1, column=6, columnspan=2, sticky="w", pady=(6, 0))

        ttk.Label(f, text="Output").grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(f, textvariable=self.out_var).grid(row=2, column=1, columnspan=5, sticky="ew",
                                                     padx=(4, 4), pady=(6, 0))
        ttk.Button(f, text="Browse…", command=self._browse).grid(row=2, column=6, pady=(6, 0))
        ttk.Button(f, text="Open folder", command=self._open_folder).grid(row=2, column=7, pady=(6, 0))
        f.columnconfigure(5, weight=1)

        bar = ttk.Frame(self, padding=(8, 0, 8, 8))
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

    def _tree(self, parent, cols):
        frame = ttk.Frame(parent)
        tree = ttk.Treeview(frame, columns=[c for c, _, _ in cols], show="headings")
        for key, title, width in cols:
            tree.heading(key, text=title, command=lambda k=key, t=tree: self._sort(t, k, False))
            tree.column(key, width=width, stretch=key in ("race_name", "comment"))
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
        rf, self.race_tree = self._tree(pane, RACE_COLS)
        uf, self.runner_tree = self._tree(pane, RUNNER_COLS)
        pane.add(rf, weight=3)
        pane.add(uf, weight=2)
        self.race_tree.bind("<<TreeviewSelect>>", self._show_runners)

    def _build_log(self):
        f = ttk.Frame(self, padding=8)
        f.pack(fill="x")
        self.log_text = tk.Text(f, height=6, state="disabled", wrap="none")
        ys = ttk.Scrollbar(f, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=ys.set)
        self.log_text.pack(side="left", fill="x", expand=True)
        ys.pack(side="left", fill="y")

    # --- actions ----------------------------------------------------------

    def _set_range(self, back, span):
        end = date.today() - timedelta(days=back)
        self.start_var.set((end - timedelta(days=span - 1)).isoformat())
        self.end_var.set(end.isoformat())

    def _browse(self):
        d = filedialog.askdirectory(initialdir=self.out_var.get())
        if d:
            self.out_var.set(d)

    def _open_folder(self):
        path = Path(self.out_var.get())
        path.mkdir(parents=True, exist_ok=True)
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        if sys.platform.startswith("win"):
            os.startfile(path)
        else:
            subprocess.Popen([opener, str(path)])

    def _start(self):
        try:
            start = date.fromisoformat(self.start_var.get().strip())
            end = date.fromisoformat(self.end_var.get().strip())
            delay = float(self.delay_var.get())
        except (ValueError, tk.TclError):
            messagebox.showerror("Invalid input", "Dates must be YYYY-MM-DD and delay a number.")
            return
        if start > end:
            messagebox.showerror("Invalid input", "'From' must not be after 'To'.")
            return
        if delay < 0.5:
            delay = 0.5  # stay polite to the site

        self.race_tree.delete(*self.race_tree.get_children())
        self.runner_tree.delete(*self.runner_tree.get_children())
        self.runners.clear()
        self.races.clear()
        self.export_btn.configure(state="disabled")
        self.stop_event.clear()
        self.progress.configure(value=0, maximum=(end - start).days + 1)
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status_var.set("Starting…")

        kwargs = dict(out=Path(self.out_var.get()), uk_ire=self.uk_ire_var.get(), delay=delay,
                      refresh=self.refresh_var.get(), breeding=self.breeding_var.get(),
                      intl_pattern=self.intl_pattern_var.get(),
                      stop=self.stop_event,
                      on_race=lambda r, u: self.q.put(("race", (r, u))),
                      on_day=lambda i, n, d: self.q.put(("day", (i, n, d))))
        self.worker = threading.Thread(target=self._run, args=(start, end), kwargs=kwargs, daemon=True)
        self.worker.start()

    def _run(self, start, end, **kwargs):
        try:
            paths = sl_results.scrape(start, end, **kwargs)
            self.q.put(("done", paths))
        except Exception as e:  # surface anything unexpected in the GUI
            self.q.put(("error", str(e)))

    def _stop(self):
        self.stop_event.set()
        self.stop_btn.configure(state="disabled")
        self.status_var.set("Stopping…")

    def _export(self):
        ExportDialog(self.winfo_toplevel(), self._rows_for, self.out_var.get())

    def _rows_for(self, dataset):
        order = self.race_tree.get_children()  # export in the order currently shown
        if dataset == "Races":
            return [self.races[i] for i in order]
        return [{**self.races[i], **r} for i in order for r in self.runners[i]]

    def _show_runners(self, _event=None):
        self.runner_tree.delete(*self.runner_tree.get_children())
        sel = self.race_tree.selection()
        if not sel:
            return
        for row in self.runners.get(sel[0], []):
            self.runner_tree.insert("", "end", values=[_fmt(row.get(c)) for c, _, _ in RUNNER_COLS])

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

    def _on_day(self, payload):
        i, n, d = payload
        self.progress.configure(value=i)
        self.status_var.set(f"Day {i + 1}/{n}: {d}")

    def _on_race(self, payload):
        race, runners = payload
        winner = next((r for r in runners if r.get("position") == 1), {})
        values = {**race, "winner": winner.get("horse"), "sp": winner.get("sp")}
        iid = self.race_tree.insert("", "end", values=[_fmt(values.get(c)) for c, _, _ in RACE_COLS])
        self.runners[iid] = runners
        self.races[iid] = race

    def _finish(self, status):
        self.progress.configure(value=self.progress["maximum"])
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.export_btn.configure(state="normal" if self.races else "disabled")
        self.status_var.set(status)

    def _on_done(self, paths):
        n = len(self.race_tree.get_children())
        self._finish(("Stopped" if self.stop_event.is_set() else "Done") + f" – {n} races")
        self._on_log(f"Saved {paths[0].name} and {paths[1].name}")

    def _on_error(self, msg):
        self._finish("Error")
        messagebox.showerror("Scrape failed", msg)


def _fmt(v):
    return "" if v is None else v


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Sporting Life Results Scraper")
        self.geometry("1300x800")
        self.minsize(800, 500)

        tabs = ttk.Notebook(self)
        tabs.pack(fill="both", expand=True)
        tabs.add(ResultsFrame(tabs), text="Scrape results")
        tabs.add(RacecardsFrame(tabs), text="Racecards")
        tabs.add(JoinCsvApp(tabs), text="Join CSV files")
        tabs.add(CsvToTableFrame(tabs), text="CSV to table")
        self.raceform = RaceformTabs(self, tabs)


if __name__ == "__main__":
    App().mainloop()
