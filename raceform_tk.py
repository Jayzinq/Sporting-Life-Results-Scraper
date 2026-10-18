#!/usr/bin/env python3
"""
Tkinter browser for large CSV and SQLite files.

Tabs:
- CSV Viewer:    pages through very large CSV files (e.g. raceform.csv) without
                 loading them into memory. A background thread indexes record
                 offsets so any page can be jumped to; supports Find and Go to row.
- SQLite Viewer: browse tables page by page (e.g. raceform.db) or run your own SQL,
                 and export any result to CSV.
- CSV -> SQLite: create (or append to) a SQLite database from a CSV, using the
                 table definition in a schema file (e.g. '.schema').

Double-click any row to see the full record (handy for long comments).

Run:
    python3 raceform_tk.py [file.csv | file.db ...]
"""
import csv
import io
import itertools
import math
import os
import queue
import sqlite3
import sys
import threading
import time
import traceback
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import tkinter.font as tkfont

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_STEP = 500                      # CSV byte offset stored every N records
PAGE_SIZES = (100, 250, 500, 1000, 2000)
DEFAULT_PAGE_SIZE = 500
MAX_DIRECT_ROWS = 10000               # cap for SQL results that can't be paged
INSERT_BATCH = 20000
NULL_TOKENS = {"", "-", "–", "—"}   # empty, hyphen, en dash, em dash

_limit = sys.maxsize
while True:
    try:
        csv.field_size_limit(_limit)
        break
    except OverflowError:
        _limit //= 10


def qi(name):
    """Quote an SQL identifier."""
    return '"' + name.replace('"', '""') + '"'


def fmt_int(n):
    return f"{n:,}"


def sniff_encoding(path):
    """Return 'utf-8' if the start of the file decodes as UTF-8, else 'cp1252'."""
    with open(path, "rb") as f:
        sample = f.read(1 << 20)
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError as e:
        if e.start < len(sample) - 4:   # not just a character cut off at the end
            return "cp1252"
    return "utf-8"


def read_header(path, encoding):
    """Return (header_fields, byte_offset_of_first_data_record)."""
    with open(path, "rb") as f:
        raw = b""
        while True:
            line = f.readline()
            if not line:
                break
            raw += line
            if raw.count(b'"') % 2 == 0:
                break
    text = raw.decode("utf-8-sig" if encoding == "utf-8" else encoding, errors="replace")
    header = next(csv.reader(io.StringIO(text)), [])
    return [h.strip() for h in header], len(raw)


def iter_csv_rows(path, offset, encoding):
    """Yield non-empty parsed CSV records starting at a byte offset."""
    fb = open(path, "rb")
    fb.seek(offset)
    with io.TextIOWrapper(fb, encoding=encoding, errors="replace", newline="") as tf:
        for row in csv.reader(tf):
            if row:
                yield row


# --------------------------------------------------------------------------- widgets

class DataGrid(ttk.Frame):
    """Treeview with scrollbars, a row-number column and a record popup."""

    def __init__(self, master):
        super().__init__(master)
        self.tree = ttk.Treeview(self, show="headings", selectmode="browse")
        vsb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(self, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.tree.bind("<Double-1>", self._show_record)
        self.tree.bind("<Return>", self._show_record)
        self.columns = []
        self.rows = []
        self.first_rownum = 1
        self._font = tkfont.nametofont("TkDefaultFont")

    def set_columns(self, cols):
        self.columns = list(cols)
        ids = ["#"] + [f"c{i}" for i in range(len(cols))]
        self.tree.configure(columns=ids, displaycolumns=ids)
        self.tree.heading("#", text="#")
        self.tree.column("#", width=70, minwidth=40, stretch=False, anchor="e")
        for i, c in enumerate(cols):
            self.tree.heading(f"c{i}", text=c, anchor="w")
            self.tree.column(f"c{i}", width=100, minwidth=40, stretch=False)

    def set_rows(self, rows, first_rownum, autosize=False):
        self.tree.delete(*self.tree.get_children())
        self.rows = rows
        self.first_rownum = first_rownum
        for i, row in enumerate(rows):
            vals = [first_rownum + i]
            for v in row:
                if v is None:
                    v = ""
                elif isinstance(v, bytes):
                    v = f"<blob {len(v)} bytes>"
                vals.append(v)
            self.tree.insert("", "end", iid=str(i), values=vals)
        if autosize:
            self._autosize()
        self.tree.yview_moveto(0)

    def _autosize(self):
        sample = self.rows[:50]
        measure = self._font.measure
        for i, c in enumerate(self.columns):
            width = measure(c) + 20
            for row in sample:
                if i < len(row) and row[i] is not None:
                    width = max(width, measure(str(row[i])[:60]) + 16)
            self.tree.column(f"c{i}", width=min(width, 400))
        last = self.first_rownum + len(self.rows)
        self.tree.column("#", width=measure(fmt_int(last)) + 20)

    def select(self, index):
        iid = str(index)
        if self.tree.exists(iid):
            self.tree.selection_set(iid)
            self.tree.focus(iid)
            self.tree.see(iid)
            self.tree.focus_set()

    def _show_record(self, event=None):
        iid = self.tree.identify_row(event.y) if event and event.type == tk.EventType.ButtonPress else self.tree.focus()
        if not iid:
            iid = self.tree.focus()
        if not iid:
            return
        i = int(iid)
        row = self.rows[i]
        win = tk.Toplevel(self)
        win.title(f"Record {fmt_int(self.first_rownum + i)}")
        win.geometry("700x600")
        txt = tk.Text(win, wrap="word")
        sb = ttk.Scrollbar(win, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(fill="both", expand=True)
        txt.tag_configure("key", font=(self._font.actual("family"), self._font.actual("size"), "bold"))
        for k, col in enumerate(self.columns):
            v = row[k] if k < len(row) else ""
            txt.insert("end", f"{col}: ", "key")
            txt.insert("end", f"{'' if v is None else v}\n")
        txt.configure(state="disabled")
        win.bind("<Escape>", lambda e: win.destroy())


class Pager(ttk.Frame):
    """First / prev / page entry / next / last controls plus a page-size chooser."""

    def __init__(self, master, on_page, on_size):
        super().__init__(master)
        self.on_page = on_page
        self.on_size = on_size
        self.page = 0
        self.pages = None
        self.page_var = tk.StringVar(value="1")
        self.of_var = tk.StringVar(value="of ?")
        self.size_var = tk.StringVar(value=str(DEFAULT_PAGE_SIZE))
        ttk.Button(self, text="⏮", width=3, command=lambda: self.go(0)).pack(side="left")
        ttk.Button(self, text="◀", width=3, command=lambda: self.go(self.page - 1)).pack(side="left")
        ttk.Label(self, text=" Page ").pack(side="left")
        ent = ttk.Entry(self, textvariable=self.page_var, width=8, justify="right")
        ent.pack(side="left")
        ent.bind("<Return>", self._entered)
        ttk.Label(self, textvariable=self.of_var, width=14).pack(side="left", padx=(4, 0))
        ttk.Button(self, text="▶", width=3, command=lambda: self.go(self.page + 1)).pack(side="left")
        ttk.Button(self, text="⏭", width=3, command=self._last).pack(side="left")
        ttk.Label(self, text="   Rows/page ").pack(side="left")
        cb = ttk.Combobox(self, textvariable=self.size_var, values=PAGE_SIZES, width=6, state="readonly")
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda e: self.on_size(int(self.size_var.get())))

    @property
    def page_size(self):
        return int(self.size_var.get())

    def set_pages(self, pages):
        self.pages = pages
        self.of_var.set("of ?" if pages is None else f"of {fmt_int(pages)}")

    def set_page(self, page):
        self.page = page
        self.page_var.set(str(page + 1))

    def go(self, page):
        if self.pages is not None:
            page = min(page, self.pages - 1)
        page = max(page, 0)
        self.on_page(page)

    def _last(self):
        if self.pages is None:
            messagebox.showinfo("Please wait", "Total row count not known yet.")
        else:
            self.go(self.pages - 1)

    def _entered(self, event=None):
        try:
            self.go(int(self.page_var.get().replace(",", "")) - 1)
        except ValueError:
            self.set_page(self.page)


# --------------------------------------------------------------------------- CSV tab

class CsvIndex:
    """Byte offsets of every INDEX_STEP-th record, filled by a background thread."""

    def __init__(self, path, start):
        self.path = path
        self.size = os.path.getsize(path)
        self.offsets = [start]
        self.rows = 0
        self.pos = start
        self.total = None
        self.cancel = threading.Event()

    def run(self):
        offsets = self.offsets
        rows, pos, in_quotes, lines = 0, self.offsets[0], False, 0
        with open(self.path, "rb") as f:
            f.seek(pos)
            for line in f:
                if not in_quotes:
                    if line in (b"\n", b"\r\n"):        # csv.reader skips blank lines too
                        pos += len(line)
                        continue
                    if rows and rows % INDEX_STEP == 0:
                        offsets.append(pos)
                if line.count(b'"') & 1:
                    in_quotes = not in_quotes
                pos += len(line)
                if not in_quotes:
                    rows += 1
                lines += 1
                if lines & 0xFFFF == 0:
                    self.rows, self.pos = rows, pos
                    if self.cancel.is_set():
                        return
        if in_quotes:          # unterminated quote at EOF still yields one record
            rows += 1
        self.rows, self.pos, self.total = rows, pos, rows


class CsvTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.index = None
        self.header = []
        self.encoding = "utf-8"
        self.search_cancel = None

        top = ttk.Frame(self)
        top.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Button(top, text="Open CSV…", command=self.choose).pack(side="left")
        self.path_var = tk.StringVar(value="No file loaded")
        ttk.Label(top, textvariable=self.path_var).pack(side="left", padx=8)

        nav = ttk.Frame(self)
        nav.pack(fill="x", padx=6, pady=2)
        self.pager = Pager(nav, self.show_page, self._size_changed)
        self.pager.pack(side="left")
        ttk.Label(nav, text="   Go to row ").pack(side="left")
        self.goto_var = tk.StringVar()
        e = ttk.Entry(nav, textvariable=self.goto_var, width=10)
        e.pack(side="left")
        e.bind("<Return>", lambda ev: self.goto_row())
        ttk.Button(nav, text="Go", width=4, command=self.goto_row).pack(side="left", padx=2)

        find = ttk.Frame(self)
        find.pack(fill="x", padx=6, pady=2)
        ttk.Label(find, text="Find ").pack(side="left")
        self.find_var = tk.StringVar()
        fe = ttk.Entry(find, textvariable=self.find_var, width=30)
        fe.pack(side="left")
        fe.bind("<Return>", lambda ev: self.find_next())
        ttk.Label(find, text=" in ").pack(side="left")
        self.find_col = ttk.Combobox(find, values=["All columns"], width=16, state="readonly")
        self.find_col.current(0)
        self.find_col.pack(side="left")
        self.case_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(find, text="Match case", variable=self.case_var).pack(side="left", padx=6)
        ttk.Button(find, text="Find next", command=self.find_next).pack(side="left")
        ttk.Button(find, text="Stop", command=self.stop_find).pack(side="left", padx=2)

        self.grid_ = DataGrid(self)
        self.grid_.pack(fill="both", expand=True, padx=6, pady=4)

        bottom = ttk.Frame(self)
        bottom.pack(fill="x", padx=6, pady=(0, 6))
        self.status = tk.StringVar(value="Open a CSV file to begin.")
        ttk.Label(bottom, textvariable=self.status).pack(side="left")
        self.progress = ttk.Progressbar(bottom, length=200, maximum=100)
        self.progress.pack(side="right")

    # -- loading
    def choose(self):
        path = filedialog.askopenfilename(
            title="Open CSV", initialdir=HERE,
            filetypes=[("CSV files", "*.csv"), ("All files", "*")])
        if path:
            self.open_csv(path)

    def open_csv(self, path):
        if self.index:
            self.index.cancel.set()
        self.stop_find()
        try:
            self.encoding = sniff_encoding(path)
            self.header, start = read_header(path, self.encoding)
        except OSError as e:
            messagebox.showerror("Open CSV", str(e))
            return
        self.index = CsvIndex(path, start)
        threading.Thread(target=self.index.run, daemon=True).start()
        size_mb = self.index.size / 1e6
        self.path_var.set(f"{path}   ({size_mb:,.1f} MB, {self.encoding})")
        self.grid_.set_columns(self.header)
        self.find_col.configure(values=["All columns"] + self.header)
        self.find_col.current(0)
        self.pager.set_pages(None)
        self.show_page(0, autosize=True)
        self._tick(self.index)

    def _tick(self, index):
        if index is not self.index:
            return
        if index.total is None:
            pct = index.pos / index.size if index.size else 1
            self.progress["value"] = pct * 100
            self.status.set(f"Indexing… {fmt_int(index.rows)} rows ({pct:.0%}). "
                            "You can browse while this runs.")
            self.after(250, self._tick, index)
        else:
            self.progress["value"] = 100
            self.pager.set_pages(max(1, math.ceil(index.total / self.pager.page_size)))
            self.status.set(f"{fmt_int(index.total)} rows · {len(self.header)} columns")

    def read_rows(self, first, count):
        idx = self.index
        k = min(first // INDEX_STEP, len(idx.offsets) - 1)
        skip = first - k * INDEX_STEP
        rows = iter_csv_rows(idx.path, idx.offsets[k], self.encoding)
        try:
            return list(itertools.islice(rows, skip, skip + count))
        finally:
            rows.close()

    def show_page(self, page, autosize=False):
        if not self.index:
            return
        n = self.pager.page_size
        if self.index.total is None and page * n > self.index.rows + INDEX_STEP:
            self.status.set("That page hasn't been indexed yet – try again shortly.")
            self.pager.set_page(self.pager.page)
            return
        self.config(cursor="watch")
        self.update_idletasks()
        try:
            rows = self.read_rows(page * n, n)
        finally:
            self.config(cursor="")
        if not rows and page > 0:
            self.pager.set_page(self.pager.page)
            return
        self.grid_.set_rows(rows, page * n + 1, autosize=autosize)
        self.pager.set_page(page)

    def _size_changed(self, size):
        if not self.index:
            return
        first = self.grid_.first_rownum - 1
        if self.index.total is not None:
            self.pager.set_pages(max(1, math.ceil(self.index.total / size)))
        self.show_page(first // size)

    def jump_to_row(self, row):
        """Show the page containing 0-based record `row` and select it."""
        n = self.pager.page_size
        page = row // n
        if self.pager.page != page or not self.grid_.rows:
            self.show_page(page)
        self.grid_.select(row - page * n)

    def goto_row(self):
        try:
            row = int(self.goto_var.get().replace(",", "")) - 1
        except ValueError:
            return
        if not self.index or row < 0:
            return
        if self.index.total is not None and row >= self.index.total:
            messagebox.showinfo("Go to row", f"The file has {fmt_int(self.index.total)} rows.")
            return
        self.jump_to_row(row)

    # -- searching
    def find_next(self):
        needle = self.find_var.get()
        if not self.index or not needle:
            return
        self.stop_find()
        sel = self.grid_.tree.focus()
        if sel:
            start = self.grid_.first_rownum - 1 + int(sel) + 1
        else:
            start = self.grid_.first_rownum - 1
        col = self.find_col.current() - 1        # -1 means all columns
        match_case = self.case_var.get()
        if not match_case:
            needle = needle.lower()
        cancel = threading.Event()
        self.search_cancel = cancel
        idx, enc = self.index, self.encoding
        app = self.app

        def work():
            k = min(start // INDEX_STEP, len(idx.offsets) - 1)
            rownum = k * INDEX_STEP
            for row in iter_csv_rows(idx.path, idx.offsets[k], enc):
                if rownum >= start:
                    if col >= 0:
                        hay = row[col] if col < len(row) else ""
                    else:
                        hay = "\x1f".join(row)
                    if not match_case:
                        hay = hay.lower()
                    if needle in hay:
                        return rownum
                rownum += 1
                if rownum % 50000 == 0:
                    if cancel.is_set():
                        return "cancelled"
                    app.call(self.status.set, f"Searching… row {fmt_int(rownum)}")
            return None

        def done(result):
            if cancel.is_set() and result != "cancelled":
                return
            self.search_cancel = None
            if result == "cancelled":
                self.status.set("Search stopped.")
            elif result is None:
                self.status.set(f"'{self.find_var.get()}' not found after row {fmt_int(start)}.")
            else:
                self.status.set(f"Found at row {fmt_int(result + 1)}.")
                self.jump_to_row(result)

        self.status.set("Searching…")
        app.run_bg(work, done)

    def stop_find(self):
        if self.search_cancel:
            self.search_cancel.set()


# --------------------------------------------------------------------------- SQLite tab

class DbTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.path = None
        self.src = None          # SQL whose results are being paged
        self.gen = 0
        self.total = None
        self.active = set()
        self.lock = threading.Lock()

        top = ttk.Frame(self)
        top.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Button(top, text="Open DB…", command=self.choose).pack(side="left")
        ttk.Label(top, text="  Table ").pack(side="left")
        self.table_cb = ttk.Combobox(top, width=24, state="readonly")
        self.table_cb.pack(side="left")
        self.table_cb.bind("<<ComboboxSelected>>", lambda e: self.browse_table())
        self.path_var = tk.StringVar(value="No database loaded")
        ttk.Label(top, textvariable=self.path_var).pack(side="left", padx=8)

        sqlf = ttk.Frame(self)
        sqlf.pack(fill="x", padx=6, pady=2)
        self.sql_text = tk.Text(sqlf, height=4, wrap="word", undo=True)
        self.sql_text.pack(side="left", fill="x", expand=True)
        self.sql_text.bind("<Control-Return>", lambda e: (self.run_sql(), "break")[1])
        btns = ttk.Frame(sqlf)
        btns.pack(side="left", padx=(6, 0), fill="y")
        ttk.Button(btns, text="Run SQL (Ctrl+Enter)", command=self.run_sql).pack(fill="x")
        ttk.Button(btns, text="Stop", command=self._user_stop).pack(fill="x", pady=2)
        ttk.Button(btns, text="Export results to CSV…", command=self.export_csv).pack(fill="x")

        nav = ttk.Frame(self)
        nav.pack(fill="x", padx=6, pady=2)
        self.pager = Pager(nav, self.load_page, self._size_changed)
        self.pager.pack(side="left")

        self.grid_ = DataGrid(self)
        self.grid_.pack(fill="both", expand=True, padx=6, pady=4)

        self.status = tk.StringVar(value="Open a SQLite database to begin.")
        ttk.Label(self, textvariable=self.status).pack(fill="x", padx=6, pady=(0, 6))

    def _connect(self):
        conn = sqlite3.connect(self.path, check_same_thread=False)
        with self.lock:
            self.active.add(conn)
        return conn

    def _close(self, conn):
        with self.lock:
            self.active.discard(conn)
        conn.close()

    def stop(self):
        with self.lock:
            for c in self.active:
                c.interrupt()

    def _user_stop(self):
        self.stop()
        self.status.set("Stopped.")

    def choose(self):
        path = filedialog.askopenfilename(
            title="Open SQLite database", initialdir=HERE,
            filetypes=[("SQLite databases", "*.db *.sqlite *.sqlite3"), ("All files", "*")])
        if path:
            self.open_db(path)

    def open_db(self, path):
        if not os.path.isfile(path):
            messagebox.showerror("Open DB", f"File not found:\n{path}")
            return
        self.path = path
        try:
            conn = sqlite3.connect(path)
            names = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            conn.close()
        except sqlite3.Error as e:
            messagebox.showerror("Open DB", f"Not a usable SQLite database:\n{e}")
            return
        self.path_var.set(f"{path}   ({os.path.getsize(path) / 1e6:,.1f} MB)")
        self.table_cb.configure(values=names)
        if names:
            self.table_cb.current(0)
            self.browse_table()
        else:
            self.status.set("Database has no tables.")

    def refresh_tables(self):
        if not self.path:
            return
        conn = sqlite3.connect(self.path)
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        conn.close()
        self.table_cb.configure(values=names)

    def browse_table(self):
        name = self.table_cb.get()
        if name:
            self.set_source(f"SELECT * FROM {qi(name)}", f"SELECT COUNT(*) FROM {qi(name)}")

    def set_source(self, src, count_sql=None):
        self.stop()
        self.gen += 1
        self.src = src
        self.total = None
        self.pager.set_pages(None)
        self._columns_key = None
        self.load_page(0)
        gen = self.gen

        def work():
            conn = self._connect()
            try:
                return conn.execute(count_sql or f"SELECT COUNT(*) FROM ({src})").fetchone()[0]
            finally:
                self._close(conn)

        def done(total):
            if gen == self.gen:
                self.total = total
                self.pager.set_pages(max(1, math.ceil(total / self.pager.page_size)))
                self._update_status()

        self.app.run_bg(work, done, lambda e: None)

    def load_page(self, page):
        if not self.src:
            return
        gen, src, n = self.gen, self.src, self.pager.page_size
        self.status.set("Loading…")

        def work():
            conn = self._connect()
            try:
                cur = conn.execute(f"SELECT * FROM ({src}) LIMIT ? OFFSET ?", (n, page * n))
                return [d[0] for d in cur.description], cur.fetchall()
            finally:
                self._close(conn)

        def done(result):
            if gen != self.gen:
                return
            cols, rows = result
            if not rows and page > 0:
                self._update_status()
                return
            new_cols = cols != self.grid_.columns
            if new_cols:
                self.grid_.set_columns(cols)
            self.grid_.set_rows(rows, page * n + 1, autosize=new_cols)
            self.pager.set_page(page)
            self._update_status()

        self.app.run_bg(work, done, self._error)

    def _update_status(self):
        total = "counting…" if self.total is None else fmt_int(self.total)
        shown = len(self.grid_.rows)
        first = self.grid_.first_rownum
        rng = f"{fmt_int(first)}–{fmt_int(first + shown - 1)}" if shown else "none"
        self.status.set(f"Rows {rng} of {total} · {len(self.grid_.columns)} columns")

    def _size_changed(self, size):
        if self.total is not None:
            self.pager.set_pages(max(1, math.ceil(self.total / size)))
        self.load_page((self.grid_.first_rownum - 1) // size)

    def _error(self, exc):
        if "interrupted" in str(exc):     # superseded or stopped by the user
            return
        self.status.set(f"Error: {exc}")
        messagebox.showerror("SQLite", str(exc))

    def run_sql(self):
        if not self.path:
            messagebox.showinfo("Run SQL", "Open a database first.")
            return
        sql = self.sql_text.get("1.0", "end").strip().rstrip(";").strip()
        if not sql:
            return
        self.stop()
        self.status.set("Running…")

        def work():
            conn = self._connect()
            try:
                try:        # can it be paged as a sub-query?
                    conn.execute(f"SELECT * FROM ({sql}) LIMIT 0")
                    return ("query",)
                except sqlite3.Error:
                    pass
                conn.isolation_level = None
                try:
                    cur = conn.execute(sql)
                except sqlite3.ProgrammingError:      # several statements
                    conn.executescript(sql + ";")
                    return ("done", -1)
                if cur.description:
                    return ("rows", [d[0] for d in cur.description], cur.fetchmany(MAX_DIRECT_ROWS))
                return ("done", cur.rowcount)
            finally:
                self._close(conn)

        def done(result):
            if result[0] == "query":
                self.set_source(sql)
            elif result[0] == "rows":
                self.gen += 1
                self.src = None
                _, cols, rows = result
                self.grid_.set_columns(cols)
                self.grid_.set_rows(rows, 1, autosize=True)
                self.pager.set_pages(1)
                self.pager.set_page(0)
                self.status.set(f"{fmt_int(len(rows))} rows (not pageable; capped at {fmt_int(MAX_DIRECT_ROWS)})")
            else:
                n = result[1]
                self.status.set("Statement(s) executed" + (f" – {fmt_int(n)} rows affected." if n >= 0 else "."))
                self.refresh_tables()

        self.app.run_bg(work, done, self._error)

    def export_csv(self):
        if not self.src:
            messagebox.showinfo("Export", "Browse a table or run a SELECT query first.")
            return
        path = filedialog.asksaveasfilename(
            title="Export results to CSV", initialdir=HERE, defaultextension=".csv",
            filetypes=[("CSV files", "*.csv")])
        if not path:
            return
        src = self.src
        app = self.app

        def work():
            conn = self._connect()
            try:
                cur = conn.execute(src)
                n = 0
                with open(path, "w", newline="", encoding="utf-8") as f:
                    w = csv.writer(f)
                    w.writerow([d[0] for d in cur.description])
                    while True:
                        rows = cur.fetchmany(10000)
                        if not rows:
                            break
                        w.writerows(rows)
                        n += len(rows)
                        app.call(self.status.set, f"Exporting… {fmt_int(n)} rows")
                return n
            finally:
                self._close(conn)

        def done(n):
            self.status.set(f"Exported {fmt_int(n)} rows to {path}")

        self.status.set("Exporting…")
        self.app.run_bg(work, done, self._error)


# --------------------------------------------------------------------------- convert tab

def parse_schema(schema_text):
    """Return {table: (create_sql, [index_sql], [columns])} for a schema script."""
    mem = sqlite3.connect(":memory:")
    try:
        mem.executescript(schema_text)
        out = {}
        for name, create in mem.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY rowid"):
            indexes = [r[0] for r in mem.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
                (name,))]
            cols = [r[1] for r in mem.execute(f"PRAGMA table_info({qi(name)})")]
            out[name] = (create, indexes, cols)
        return out
    finally:
        mem.close()


class ConvertTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.tables = {}
        self.cancel = None

        form = ttk.Frame(self)
        form.pack(fill="x", padx=10, pady=10)
        form.columnconfigure(1, weight=1)

        default_csv = os.path.join(HERE, "raceform.csv")
        default_schema = os.path.join(HERE, ".schema")
        self.csv_var = tk.StringVar(value=default_csv if os.path.isfile(default_csv) else "")
        self.schema_var = tk.StringVar(value=default_schema if os.path.isfile(default_schema) else "")
        self.db_var = tk.StringVar(value=os.path.splitext(self.csv_var.get())[0] + ".db" if self.csv_var.get() else "")

        ttk.Label(form, text="CSV file").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(form, textvariable=self.csv_var).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(form, text="Browse…", command=self.pick_csv).grid(row=0, column=2)

        ttk.Label(form, text="Schema file").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Entry(form, textvariable=self.schema_var).grid(row=1, column=1, sticky="ew", padx=6)
        ttk.Button(form, text="Browse…", command=self.pick_schema).grid(row=1, column=2)

        ttk.Label(form, text="Output database").grid(row=2, column=0, sticky="w", pady=3)
        ttk.Entry(form, textvariable=self.db_var).grid(row=2, column=1, sticky="ew", padx=6)
        ttk.Button(form, text="Browse…", command=self.pick_db).grid(row=2, column=2)

        ttk.Label(form, text="Table").grid(row=3, column=0, sticky="w", pady=3)
        tf = ttk.Frame(form)
        tf.grid(row=3, column=1, sticky="w", padx=6)
        self.table_cb = ttk.Combobox(tf, width=24, state="readonly")
        self.table_cb.pack(side="left")
        ttk.Button(tf, text="Reload schema", command=self.load_schema).pack(side="left", padx=6)

        self.null_var = tk.BooleanVar(value=True)
        self.replace_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(form, text="Store empty, '-' and '–' values as NULL",
                        variable=self.null_var).grid(row=4, column=1, sticky="w", padx=6)
        ttk.Checkbutton(form, text="Replace the table if it already exists (otherwise rows are appended)",
                        variable=self.replace_var).grid(row=5, column=1, sticky="w", padx=6)

        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=10)
        self.convert_btn = ttk.Button(btns, text="Create database", command=self.convert)
        self.convert_btn.pack(side="left")
        self.cancel_btn = ttk.Button(btns, text="Cancel", command=self.do_cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=6)
        self.open_btn = ttk.Button(btns, text="Open in SQLite Viewer", state="disabled",
                                   command=lambda: self.app.show_db(self.db_var.get()))
        self.open_btn.pack(side="left")

        self.progress = ttk.Progressbar(self, maximum=100)
        self.progress.pack(fill="x", padx=10, pady=8)

        logf = ttk.Frame(self)
        logf.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.log_text = tk.Text(logf, height=12, wrap="word", state="disabled")
        sb = ttk.Scrollbar(logf, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log_text.pack(fill="both", expand=True)

        if self.schema_var.get():
            self.load_schema(quiet=True)

    def log(self, msg):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", time.strftime("%H:%M:%S  ") + msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def pick_csv(self):
        p = filedialog.askopenfilename(title="CSV file", initialdir=HERE,
                                       filetypes=[("CSV files", "*.csv"), ("All files", "*")])
        if p:
            self.csv_var.set(p)
            if not self.db_var.get():
                self.db_var.set(os.path.splitext(p)[0] + ".db")

    def pick_schema(self):
        p = filedialog.askopenfilename(title="Schema file", initialdir=HERE,
                                       filetypes=[("All files", "*"), ("SQL files", "*.sql")])
        if p:
            self.schema_var.set(p)
            self.load_schema()

    def pick_db(self):
        p = filedialog.asksaveasfilename(title="Output database", initialdir=HERE,
                                         defaultextension=".db", confirmoverwrite=False,
                                         filetypes=[("SQLite databases", "*.db"), ("All files", "*")])
        if p:
            self.db_var.set(p)

    def load_schema(self, quiet=False):
        path = self.schema_var.get()
        try:
            with open(path, encoding="utf-8") as f:
                self.tables = parse_schema(f.read())
        except (OSError, sqlite3.Error) as e:
            self.tables = {}
            self.table_cb.configure(values=[])
            self.table_cb.set("")
            if not quiet:
                messagebox.showerror("Schema", f"Could not read schema:\n{e}")
            return False
        names = list(self.tables)
        self.table_cb.configure(values=names)
        if names:
            self.table_cb.current(0)
        for n in names:
            self.log(f"Schema: table '{n}' with {len(self.tables[n][2])} columns.")
        return bool(names)

    def convert(self):
        csv_path, db_path = self.csv_var.get(), self.db_var.get()
        if not os.path.isfile(csv_path):
            messagebox.showerror("Convert", "Choose an existing CSV file.")
            return
        if not db_path:
            messagebox.showerror("Convert", "Choose an output database file.")
            return
        if os.path.abspath(db_path) == os.path.abspath(csv_path):
            messagebox.showerror("Convert", "The output database can't be the CSV file.")
            return
        if not self.load_schema():
            messagebox.showerror("Convert", "The schema file defines no tables.")
            return
        table = self.table_cb.get()
        replace = self.replace_var.get()

        if os.path.exists(db_path):
            try:
                conn = sqlite3.connect(db_path)
                exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                      (table,)).fetchone()
                conn.close()
            except sqlite3.Error as e:
                messagebox.showerror("Convert", f"{db_path} is not a usable SQLite database:\n{e}")
                return
            if exists:
                verb = "REPLACE (all existing rows deleted)" if replace else "APPEND rows to"
                if not messagebox.askyesno(
                        "Table exists",
                        f"Table '{table}' already exists in\n{db_path}\n\n{verb} it?"):
                    return

        create_sql, index_sqls, cols = self.tables[table]
        nullify = self.null_var.get()
        cancel = threading.Event()
        self.cancel = cancel
        self.convert_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.open_btn.configure(state="disabled")
        self.progress["value"] = 0
        self.log(f"Converting {csv_path} → {db_path} (table '{table}')")
        app = self.app

        def work():
            t0 = time.monotonic()
            enc = sniff_encoding(csv_path)
            size = os.path.getsize(csv_path)
            conn = sqlite3.connect(db_path, isolation_level=None)
            try:
                conn.execute("PRAGMA synchronous=OFF")
                conn.execute("PRAGMA cache_size=-200000")
                conn.execute("BEGIN")
                exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                      (table,)).fetchone()
                if exists and replace:
                    conn.execute(f"DROP TABLE {qi(table)}")
                if not exists or replace:
                    conn.execute(create_sql)
                    target_cols = cols
                else:
                    target_cols = [r[1] for r in conn.execute(f"PRAGMA table_info({qi(table)})")]

                with open(csv_path, "rb") as fb:
                    tf = io.TextIOWrapper(fb, encoding="utf-8-sig" if enc == "utf-8" else enc,
                                          errors="replace", newline="")
                    reader = csv.reader(tf)
                    header = next(reader, [])
                    hmap = {h.strip().strip("[]\"").lower(): i for i, h in enumerate(header)}
                    used = [(c, hmap[c.lower()]) for c in target_cols if c.lower() in hmap]
                    missing = [c for c in target_cols if c.lower() not in hmap]
                    lower_cols = {c.lower() for c in target_cols}
                    extra = [h for h in header if h.strip().strip("[]\"").lower() not in lower_cols]
                    if not used:
                        raise ValueError("None of the CSV header names match the table's columns.")
                    app.call(self.log, f"Matched {len(used)} of {len(target_cols)} columns "
                                       f"(encoding {enc}).")
                    if missing:
                        app.call(self.log, "Not in CSV (left NULL): " + ", ".join(missing))
                    if extra:
                        app.call(self.log, "CSV columns not in schema (skipped): " + ", ".join(extra))

                    idx = [i for _, i in used]
                    width = max(idx) + 1
                    sql = (f"INSERT INTO {qi(table)} ({', '.join(qi(c) for c, _ in used)}) "
                           f"VALUES ({', '.join('?' * len(used))})")
                    batch, n = [], 0
                    for row in reader:
                        if not row:
                            continue
                        if len(row) < width:
                            row += [""] * (width - len(row))
                        if nullify:
                            batch.append([None if row[i].strip() in NULL_TOKENS else row[i] for i in idx])
                        else:
                            batch.append([row[i] for i in idx])
                        if len(batch) >= INSERT_BATCH:
                            conn.executemany(sql, batch)
                            n += len(batch)
                            batch.clear()
                            if cancel.is_set():
                                conn.execute("ROLLBACK")
                                return None
                            pct = fb.tell() / size * 100 if size else 100
                            app.call(self._progress, pct, n)
                    if batch:
                        conn.executemany(sql, batch)
                        n += len(batch)

                if (not exists or replace) and index_sqls:
                    app.call(self.log, f"Creating {len(index_sqls)} index(es)…")
                    for s in index_sqls:
                        conn.execute(s)
                conn.execute("COMMIT")
                return n, time.monotonic() - t0
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

        def done(result):
            self._finish()
            if result is None:
                self.log("Cancelled – no changes were saved.")
                return
            n, secs = result
            self.progress["value"] = 100
            self.log(f"Done: {fmt_int(n)} rows inserted in {secs:,.1f}s.")
            self.open_btn.configure(state="normal")

        def failed(exc):
            self._finish()
            self.log(f"Failed: {exc}")
            messagebox.showerror("Convert", str(exc))

        app.run_bg(work, done, failed)

    def _progress(self, pct, n):
        self.progress["value"] = pct
        self.log_status(f"{fmt_int(n)} rows inserted ({pct:.0f}%)")

    def log_status(self, msg):
        """Overwrite the last log line if it is a progress line, otherwise append."""
        self.log_text.configure(state="normal")
        last = self.log_text.get("end-2l", "end-1c")
        if last.endswith("%)"):
            self.log_text.delete("end-2l", "end-1c")
        self.log_text.insert("end", time.strftime("%H:%M:%S  ") + msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def do_cancel(self):
        if self.cancel:
            self.cancel.set()
            self.log("Cancelling…")

    def _finish(self):
        self.cancel = None
        self.convert_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")


# --------------------------------------------------------------------------- app

class RaceformTabs:
    """Adds the CSV Viewer, SQLite Viewer and CSV -> SQLite tabs to a notebook.

    Provides the call/run_bg/show_db services the tabs use, so the tabs can be
    hosted in this script's own window or embedded in another app (gui.py).
    """

    def __init__(self, root, nb):
        self.root = root
        self.nb = nb
        self._queue = queue.Queue()
        _show_hidden_files_option(root)

        self.csv_tab = CsvTab(nb, self)
        self.db_tab = DbTab(nb, self)
        self.convert_tab = ConvertTab(nb, self)
        nb.add(self.csv_tab, text="CSV Viewer")
        nb.add(self.db_tab, text="SQLite Viewer")
        nb.add(self.convert_tab, text="CSV → SQLite")
        root.after(50, self._drain)

    def open_paths(self, paths):
        for arg in paths:
            if arg.lower().endswith(".csv"):
                self.csv_tab.open_csv(arg)
                self.nb.select(self.csv_tab)
            else:
                self.show_db(arg)

    def show_db(self, path):
        self.db_tab.open_db(path)
        self.nb.select(self.db_tab)

    def call(self, fn, *args):
        """Run fn(*args) on the Tk thread (safe to call from worker threads)."""
        self._queue.put((fn, args))

    def _drain(self):
        try:
            while True:
                fn, args = self._queue.get_nowait()
                try:
                    fn(*args)
                except Exception:
                    traceback.print_exc()
        except queue.Empty:
            pass
        self.root.after(50, self._drain)

    def run_bg(self, work, done=None, error=None):
        """Run work() in a thread, then done(result) or error(exc) on the Tk thread."""
        def runner():
            try:
                result = work()
            except Exception as e:
                if not isinstance(e, sqlite3.OperationalError) or "interrupted" not in str(e):
                    traceback.print_exc()
                self.call(error or (lambda exc: messagebox.showerror("Error", str(exc))), e)
            else:
                if done:
                    self.call(done, result)
        threading.Thread(target=runner, daemon=True).start()


def _show_hidden_files_option(root):
    """Add a 'Show hidden files' toggle to the X11 file dialogs (needed for '.schema')."""
    try:
        try:
            root.tk.call("tk_getOpenFile", "-foobarbaz")
        except tk.TclError:
            pass
        root.tk.call("set", "::tk::dialog::file::showHiddenBtn", "1")
        root.tk.call("set", "::tk::dialog::file::showHiddenVar", "0")
    except tk.TclError:
        pass


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Raceform CSV / SQLite Browser")
        self.geometry("1300x800")

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True)
        self.raceform = RaceformTabs(self, self.nb)
        self.raceform.open_paths(sys.argv[1:])


if __name__ == "__main__":
    App().mainloop()
