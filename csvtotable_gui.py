#!/usr/bin/env python3
"""Tkinter front-end for csvtotable.

Runs `csvtotable` to build the HTML, then injects a small <style> block so the
heading text and colours can be customised. Settings are remembered between runs.
"""
import json
import os
import shutil
import subprocess
import sys
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, ttk

SETTINGS_FILE = Path.home() / ".csvtotable_gui.json"

# (key, label, csvtotable's default)
COLOURS = [
    ("page_bg", "Page background", "#ffffff"),
    ("table_bg", "Table background", "#ffffff"),
    ("heading", "Heading text", "#003399"),
    ("header_text", "Column header text", "#003399"),
    ("header_bg", "Column header background", "#ffffff"),
    ("header_line", "Line under headers", "#6678b1"),
    ("cell_text", "Cell text", "#666699"),
    ("row_line", "Row divider lines", "#cccccc"),
    ("hover_text", "Row hover text", "#000099"),
    ("accent", "Buttons / current page", "#003399"),
]
DEFAULT_COLOURS = {key: default for key, _, default in COLOURS}


def find_csvtotable():
    """Prefer the copy next to the running Python (i.e. same virtualenv)."""
    local = Path(sys.executable).parent / "csvtotable"
    if local.exists():
        return str(local)
    return shutil.which("csvtotable") or str(Path.home() / "virt/bin/csvtotable")


def build_css(c, heading_size):
    return f"""
<style id="csvtotable-gui-overrides">
    body {{ background: {c['page_bg']} !important; }}
    #table_wrapper table {{ background: {c['table_bg']} !important; }}
    #table_wrapper table caption {{ color: {c['heading']} !important; font-size: {heading_size}px !important; }}
    #table_wrapper table th {{
        color: {c['header_text']} !important;
        background: {c['header_bg']} !important;
        border-bottom-color: {c['header_line']} !important;
    }}
    #table_wrapper table td {{ color: {c['cell_text']} !important; border-bottom-color: {c['row_line']} !important; }}
    #table_wrapper table tbody tr:hover td {{ color: {c['hover_text']} !important; }}
    #table_wrapper .dataTables_paginate a.current, .dt-button {{ background: {c['accent']} !important; }}
    .dt-button-info {{ color: {c['accent']} !important; }}
</style>
"""


class CsvToTableFrame(ttk.Frame):
    def __init__(self, master):
        super().__init__(master)

        settings = self.load_settings()
        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.heading_var = tk.StringVar(value=settings.get("heading", ""))
        self.heading_size_var = tk.IntVar(value=settings.get("heading_size", 20))
        self.pagination_var = tk.BooleanVar(value=settings.get("pagination", False))
        self.export_var = tk.BooleanVar(value=settings.get("export", False))
        self.open_var = tk.BooleanVar(value=settings.get("open_after", True))
        self.colours = {**DEFAULT_COLOURS, **settings.get("colours", {})}
        self.swatches = {}

        self.build_ui()

    # ---------- UI ----------
    def build_ui(self):
        pad = {"padx": 6, "pady": 4}
        files = ttk.LabelFrame(self, text="Files")
        files.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 5))

        ttk.Label(files, text="Input CSV:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(files, textvariable=self.input_var, width=50).grid(row=0, column=1, **pad)
        ttk.Button(files, text="Browse…", command=self.pick_input).grid(row=0, column=2, **pad)

        ttk.Label(files, text="Output HTML:").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(files, textvariable=self.output_var, width=50).grid(row=1, column=1, **pad)
        ttk.Button(files, text="Browse…", command=self.pick_output).grid(row=1, column=2, **pad)

        opts = ttk.LabelFrame(self, text="Heading & options")
        opts.grid(row=1, column=0, sticky="ew", padx=10, pady=5)

        ttk.Label(opts, text="Heading:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(opts, textvariable=self.heading_var, width=40).grid(row=0, column=1, columnspan=2, sticky="w", **pad)
        ttk.Label(opts, text="Size (px):").grid(row=0, column=3, sticky="e", **pad)
        ttk.Spinbox(opts, from_=10, to=60, textvariable=self.heading_size_var, width=5).grid(row=0, column=4, **pad)

        ttk.Checkbutton(opts, text="Pagination", variable=self.pagination_var).grid(row=1, column=0, sticky="w", **pad)
        ttk.Checkbutton(opts, text="Export buttons", variable=self.export_var).grid(row=1, column=1, sticky="w", **pad)
        ttk.Checkbutton(opts, text="Open in browser when done", variable=self.open_var).grid(row=1, column=2, columnspan=3, sticky="w", **pad)

        cols = ttk.LabelFrame(self, text="Colours (click a swatch to change)")
        cols.grid(row=2, column=0, sticky="ew", padx=10, pady=5)
        for i, (key, label, _) in enumerate(COLOURS):
            r, c = divmod(i, 2)
            swatch = tk.Label(cols, width=4, relief="ridge", bg=self.colours[key], cursor="hand2")
            swatch.grid(row=r, column=c * 2, **pad)
            swatch.bind("<Button-1>", lambda e, k=key: self.pick_colour(k))
            ttk.Label(cols, text=label).grid(row=r, column=c * 2 + 1, sticky="w", **pad)
            self.swatches[key] = swatch
        ttk.Button(cols, text="Reset colours", command=self.reset_colours).grid(
            row=len(COLOURS) // 2 + 1, column=0, columnspan=4, sticky="w", **pad)

        ttk.Button(self, text="Convert", command=self.convert).grid(row=3, column=0, pady=(5, 10))

    def pick_input(self):
        path = filedialog.askopenfilename(filetypes=[("CSV files", "*.csv"), ("All files", "*")])
        if path:
            self.input_var.set(path)
            if not self.output_var.get():
                self.output_var.set(str(Path(path).with_suffix(".html")))
            if not self.heading_var.get():
                self.heading_var.set(Path(path).stem)

    def pick_output(self):
        path = filedialog.asksaveasfilename(defaultextension=".html", filetypes=[("HTML files", "*.html")])
        if path:
            self.output_var.set(path)

    def pick_colour(self, key):
        _, hex_colour = colorchooser.askcolor(color=self.colours[key], parent=self)
        if hex_colour:
            self.colours[key] = hex_colour
            self.swatches[key].config(bg=hex_colour)

    def reset_colours(self):
        self.colours = dict(DEFAULT_COLOURS)
        for key, swatch in self.swatches.items():
            swatch.config(bg=self.colours[key])

    # ---------- settings ----------
    def load_settings(self):
        try:
            return json.loads(SETTINGS_FILE.read_text())
        except (OSError, ValueError):
            return {}

    def save_settings(self):
        data = {
            "heading": self.heading_var.get(),
            "heading_size": self.heading_size_var.get(),
            "pagination": self.pagination_var.get(),
            "export": self.export_var.get(),
            "open_after": self.open_var.get(),
            "colours": self.colours,
        }
        try:
            SETTINGS_FILE.write_text(json.dumps(data, indent=2))
        except OSError:
            pass

    # ---------- conversion ----------
    def convert(self):
        src = self.input_var.get().strip()
        dst = self.output_var.get().strip()
        if not src or not os.path.isfile(src):
            messagebox.showerror("CSV to Table", "Please choose an input CSV file.")
            return
        if not dst:
            messagebox.showerror("CSV to Table", "Please choose an output HTML file.")
            return
        try:
            heading_size = int(self.heading_size_var.get())
        except (tk.TclError, ValueError):
            heading_size = 20

        cmd = [find_csvtotable(), "-o"]
        if self.heading_var.get().strip():
            cmd += ["-c", self.heading_var.get().strip()]
        if self.pagination_var.get():
            cmd.append("-p")
        if self.export_var.get():
            cmd.append("-e")
        cmd += [src, dst]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
        except FileNotFoundError:
            messagebox.showerror("CSV to Table", f"Couldn't find csvtotable at:\n{cmd[0]}")
            return
        if result.returncode != 0:
            messagebox.showerror("CSV to Table", result.stderr or result.stdout or "csvtotable failed.")
            return

        # Inject colour overrides; placed last in <body> so they beat the built-in styles.
        html = Path(dst).read_text(encoding="utf-8")
        css = build_css(self.colours, heading_size)
        if "</body>" in html:
            html = html.replace("</body>", css + "</body>", 1)
        else:
            html += css
        Path(dst).write_text(html, encoding="utf-8")

        self.save_settings()
        if self.open_var.get():
            webbrowser.open(Path(dst).resolve().as_uri())
        else:
            messagebox.showinfo("CSV to Table", f"Saved:\n{dst}")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("CSV to Table")
        self.resizable(False, False)
        CsvToTableFrame(self).pack(fill="both", expand=True)


if __name__ == "__main__":
    App().mainloop()
