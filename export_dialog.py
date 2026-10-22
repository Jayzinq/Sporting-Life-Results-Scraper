"""CSV export dialog: choose which fields to include, their order and their header names."""

import csv
import json
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import sl_results

CONFIG_PATH = Path(__file__).resolve().parent / "export_config.json"

# Runner rows can carry their race's details too; shared keys (race_id, date, ...) hold the same values.
DATASETS = {
    "Runners": sl_results.RUNNER_FIELDS + sl_results.RP_RUNNER_FIELDS
               + [f for f in sl_results.RACE_FIELDS if f not in sl_results.RUNNER_FIELDS]
               + sl_results.RP_RACE_FIELDS,
    "Races": sl_results.RACE_FIELDS + sl_results.RP_RACE_FIELDS,
}
DEFAULT_INCLUDED = {"Runners": set(sl_results.RUNNER_FIELDS), "Races": set(sl_results.RACE_FIELDS)}

TICK, NOTICK = "☑", "☐"
# Blank columns export as empty cells, for lining the CSV up with other spreadsheets.
BLANK_PREFIX = "__blank_"
DEFAULT_BLANKS = 5

# Column layout of a Racing Post (rpscrape) results sheet: (our field or None for blank, header).
RP_LAYOUT = [
    ("date", "date"), ("rp_course", "course"), ("race_id", "race_id"), ("time", "off"),
    ("race_name", "race_name"), ("rp_type", "type"), ("rp_class", "class"),
    ("rp_pattern", "pattern"), ("rp_rating_band", "rating_band"), ("rp_age_band", "age_band"),
    ("rp_sex_rest", "sex_rest"), ("rp_dist", "dist"), ("rp_going", "going"), ("ran", "ran"),
    ("cloth", "num"), ("rp_pos", "pos"), ("draw", "draw"), ("rp_ovr_btn", "ovr_btn"),
    ("rp_btn", "btn"), ("horse", "horse"), ("horse_age", "age"), ("rp_sex", "sex"),
    ("weight", "wgt"), ("rp_hg", "hg"), ("rp_time", "time"), ("sp", "sp"), ("jockey", "jockey"),
    ("trainer", "trainer"), ("rp_prize", "prize"), ("official_rating", "or"), (None, "rpr"),
    (None, "ts"), ("sire", "sire"), ("dam", "dam"), ("damsire", "damsire"), ("owner", "owner"),
    ("comment", "comment"),
]


def load_config():
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    except OSError:
        pass


def is_blank(col):
    return col["field"].startswith(BLANK_PREFIX)


def blank_column(n):
    return {"field": f"{BLANK_PREFIX}{n}", "header": "", "include": True}


def default_columns(dataset, datasets=DATASETS, default_included=DEFAULT_INCLUDED):
    return ([{"field": f, "header": f, "include": f in default_included[dataset]}
             for f in datasets[dataset]]
            + [blank_column(n) for n in range(1, DEFAULT_BLANKS + 1)])


def columns_for(dataset, cfg, datasets=DATASETS, default_included=DEFAULT_INCLUDED):
    """Saved column layout for a dataset, reconciled with the fields that currently exist."""
    if not cfg.get(dataset):
        return default_columns(dataset, datasets, default_included)
    known = set(datasets[dataset])
    saved = [c for c in cfg[dataset] if c.get("field") in known or is_blank(c)]
    seen = {c["field"] for c in saved}
    return saved + [c for c in default_columns(dataset, datasets, default_included)
                    if c["field"] not in seen and not is_blank(c)]


class ExportDialog(tk.Toplevel):
    """get_rows(dataset) must return a list of dicts for "Runners" or "Races".

    datasets/default_included default to the results fields; pass others (e.g. racecards)
    with a config_key so their column setup is saved separately in export_config.json.
    """

    def __init__(self, parent, get_rows, initialdir, datasets=None, default_included=None,
                 config_key=None, file_prefix=""):
        super().__init__(parent)
        self.title("Export CSV")
        self.geometry("580x620")
        self.transient(parent)
        self.get_rows = get_rows
        self.initialdir = initialdir
        self.datasets = datasets or DATASETS
        self.default_included = default_included or DEFAULT_INCLUDED
        self.file_prefix = file_prefix
        self.full_cfg = load_config()
        self.cfg = self.full_cfg.setdefault(config_key, {}) if config_key else self.full_cfg

        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="Dataset").pack(side="left")
        ds = self.cfg.get("_dataset", "Runners")
        self.dataset_var = tk.StringVar(value=ds if ds in self.datasets else next(iter(self.datasets)))
        cb = ttk.Combobox(top, textvariable=self.dataset_var, values=list(self.datasets),
                          state="readonly", width=12)
        cb.pack(side="left", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: self._switch_dataset())
        self.count_var = tk.StringVar()
        ttk.Label(top, textvariable=self.count_var).pack(side="left", padx=8)
        self.exclude_nr_var = tk.BooleanVar(value=self.cfg.get("_exclude_nr", False))
        ttk.Checkbutton(top, text="Exclude non-runners", variable=self.exclude_nr_var
                        ).pack(side="right")

        mid = ttk.Frame(self, padding=(8, 0))
        mid.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(mid, columns=("inc", "field", "header"), show="headings",
                                 selectmode="browse")
        for key, title, width in [("inc", "Include", 60), ("field", "Field", 170),
                                  ("header", "CSV header", 220)]:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, stretch=key == "header",
                             anchor="center" if key == "inc" else "w")
        ys = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ys.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ys.pack(side="left", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._load_selected())
        self.tree.bind("<Double-1>", self._on_double)
        self.tree.bind("<space>", lambda e: self._toggle())

        side = ttk.Frame(mid, padding=(8, 0, 0, 0))
        side.pack(side="left", fill="y")
        buttons = [("Top", lambda: self._move("top")), ("Up", lambda: self._move(-1)),
                   ("Down", lambda: self._move(1)), ("Bottom", lambda: self._move("bottom")),
                   (None, None), ("Toggle", self._toggle), ("All", lambda: self._set_all(True)),
                   ("None", lambda: self._set_all(False)), (None, None),
                   ("Add blank", self._add_blank), ("Remove", self._remove_blank), (None, None)]
        if self.datasets is DATASETS:  # the Racing Post layout only fits results
            buttons.append(("RP layout", self._rp_layout))
        buttons.append(("Reset", self._reset))
        for label, cmd in buttons:
            if label is None:
                ttk.Separator(side).pack(fill="x", pady=6)
            else:
                ttk.Button(side, text=label, command=cmd, width=9).pack(pady=2)

        edit = ttk.Frame(self, padding=8)
        edit.pack(fill="x")
        ttk.Label(edit, text="Header for selected").pack(side="left")
        self.header_var = tk.StringVar()
        self.header_entry = ttk.Entry(edit, textvariable=self.header_var)
        self.header_entry.pack(side="left", fill="x", expand=True, padx=6)
        self.header_var.trace_add("write", lambda *a: self._header_changed())

        hint = ttk.Frame(self, padding=(8, 0))
        hint.pack(fill="x")
        bottom = ttk.Frame(self, padding=8)
        bottom.pack(fill="x")
        ttk.Label(hint, text="Double-click Include to toggle, or a header to edit it.\n"
                               "Blank columns export empty cells; move them where you need gaps.",
                  foreground="grey").pack(side="left")
        ttk.Button(bottom, text="Cancel", command=self._close).pack(side="right")
        ttk.Button(bottom, text="Save CSV…", command=self._save).pack(side="right", padx=6)

        self.protocol("WM_DELETE_WINDOW", self._close)
        self._loading = False
        self._switch_dataset(initial=True)

    # --- column list ------------------------------------------------------

    def _switch_dataset(self, initial=False):
        if not initial:
            self._remember()
        ds = self.dataset_var.get()
        self.columns = columns_for(ds, self.cfg, self.datasets, self.default_included)
        self.count_var.set(f"{len(self.get_rows(ds))} rows")
        self._render()

    def _render(self, select=None):
        self.tree.delete(*self.tree.get_children())
        for i, c in enumerate(self.columns):
            self.tree.insert("", "end", iid=str(i), values=self._row_values(c))
        if select is not None:
            self.tree.selection_set(str(select))
            self.tree.see(str(select))

    @staticmethod
    def _row_values(c):
        return (TICK if c["include"] else NOTICK, "(blank)" if is_blank(c) else c["field"], c["header"])

    def _selected(self):
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    def _refresh_row(self, i):
        self.tree.item(str(i), values=self._row_values(self.columns[i]))

    def _load_selected(self):
        i = self._selected()
        self._loading = True
        self.header_var.set(self.columns[i]["header"] if i is not None else "")
        self._loading = False

    def _header_changed(self):
        i = self._selected()
        if self._loading or i is None:
            return
        self.columns[i]["header"] = self.header_var.get()
        self._refresh_row(i)

    def _on_double(self, event):
        row = self.tree.identify_row(event.y)
        if not row:
            return
        self.tree.selection_set(row)
        if self.tree.identify_column(event.x) == "#1":
            self._toggle()
        else:
            self.header_entry.focus_set()
            self.header_entry.select_range(0, "end")

    def _toggle(self):
        i = self._selected()
        if i is not None:
            self.columns[i]["include"] = not self.columns[i]["include"]
            self._refresh_row(i)

    def _set_all(self, value):
        for c in self.columns:
            c["include"] = value
        self._render(self._selected())

    def _move(self, where):
        i = self._selected()
        if i is None:
            return
        c = self.columns.pop(i)
        if where == "top":
            j = 0
        elif where == "bottom":
            j = len(self.columns)
        else:
            j = min(max(i + where, 0), len(self.columns))
        self.columns.insert(j, c)
        self._render(j)

    def _add_blank(self):
        n = 1 + max((int(c["field"][len(BLANK_PREFIX):]) for c in self.columns if is_blank(c)),
                    default=0)
        i = self._selected()
        j = len(self.columns) if i is None else i + 1
        self.columns.insert(j, blank_column(n))
        self._render(j)

    def _remove_blank(self):
        i = self._selected()
        if i is None or not is_blank(self.columns[i]):
            messagebox.showinfo("Remove", "Only blank columns can be removed; untick others instead.",
                                parent=self)
            return
        del self.columns[i]
        self._render(min(i, len(self.columns) - 1) if self.columns else None)

    def _rp_layout(self):
        """Match a Racing Post results sheet: same columns, order and headers."""
        self.dataset_var.set("Runners")
        self.count_var.set(f"{len(self.get_rows('Runners'))} rows")
        cols, used, n = [], set(), 0
        for field, header in RP_LAYOUT:
            if field is None:
                n += 1
                col = blank_column(n)
                col["header"] = header
            else:
                col = {"field": field, "header": header, "include": True}
                used.add(field)
            cols.append(col)
        cols += [{"field": f, "header": f, "include": False}
                 for f in self.datasets["Runners"] if f not in used]
        self.columns = cols
        self.exclude_nr_var.set(True)
        self._render(0)

    def _reset(self):
        self.columns = default_columns(self.dataset_var.get(), self.datasets, self.default_included)
        self._render()

    # --- save -------------------------------------------------------------

    def _remember(self):
        self.cfg[self.dataset_var.get()] = self.columns
        self.cfg["_dataset"] = self.dataset_var.get()
        self.cfg["_exclude_nr"] = self.exclude_nr_var.get()
        save_config(self.full_cfg)

    def _close(self):
        self._remember()
        self.destroy()

    def _save(self):
        cols = [c for c in self.columns if c["include"]]
        if not cols:
            messagebox.showerror("Nothing to export", "Include at least one column.", parent=self)
            return
        headers = [c["header"].strip() if is_blank(c) else c["header"].strip() or c["field"]
                   for c in cols]
        named = [h for h in headers if h]
        if len(set(named)) != len(named):
            messagebox.showerror("Duplicate headers", "Each CSV header must be unique "
                                 "(blank columns may have empty headers).", parent=self)
            return
        ds = self.dataset_var.get()
        rows = self.get_rows(ds)
        if ds == "Runners" and self.exclude_nr_var.get():
            rows = [r for r in rows if r.get("status") not in sl_results.NON_RUNNER]
        if not rows:
            messagebox.showerror("No data", "Scrape some results first.", parent=self)
            return
        path = filedialog.asksaveasfilename(
            parent=self, initialdir=self.initialdir, defaultextension=".csv",
            initialfile=f"{self.file_prefix}{ds.lower()}.csv", filetypes=[("CSV", "*.csv"), ("All files", "*")])
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(headers)
            for r in rows:
                w.writerow(["" if is_blank(c) or r.get(c["field"]) is None else r.get(c["field"])
                            for c in cols])
        self._remember()
        messagebox.showinfo("Exported", f"Saved {len(rows)} rows to\n{path}", parent=self)
