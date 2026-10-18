#!/usr/bin/env python3
"""Tkinter front-end for join_csv_files.

Pick some CSV files, put them in order, choose where to save, and they are
combined into one file keeping only the first file's header row.
"""
import csv
import os
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from join_csv_files import combine_csv_files

CSV_TYPES = [("CSV files", "*.csv"), ("All files", "*.*")]


def read_header(path):
    """Return the first row of a CSV file (raises StopIteration if it's empty)."""
    with open(path, newline="", encoding="utf-8") as f:
        return next(csv.reader(f))


def header_mismatches(paths):
    """Return (path, header) for every file whose header differs from the first file's."""
    first = read_header(paths[0])
    result = []
    for path in paths[1:]:
        header = read_header(path)
        if header != first:
            result.append((path, header))
    return result


class JoinCsvApp(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=12)
        self.output_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Add the CSV files you want to join.")
        self.last_dir = str(Path.cwd())
        self._build()

    def _build(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        ttk.Label(self, text="Input files (joined top to bottom):").grid(
            row=0, column=0, columnspan=2, sticky="w")

        list_frame = ttk.Frame(self)
        list_frame.grid(row=1, column=0, sticky="nsew", pady=(4, 8))
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        self.file_list = tk.Listbox(list_frame, selectmode=tk.EXTENDED, height=12,
                                    activestyle="none")
        self.file_list.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.file_list.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.file_list.configure(yscrollcommand=scroll.set)
        self.file_list.bind("<Delete>", lambda e: self.remove_selected())

        buttons = ttk.Frame(self)
        buttons.grid(row=1, column=1, sticky="n", padx=(8, 0), pady=(4, 8))
        for text, command in [
            ("Add files…", self.add_files),
            ("Remove", self.remove_selected),
            ("Move up", lambda: self.move_selected(-1)),
            ("Move down", lambda: self.move_selected(1)),
            ("Clear", self.clear_files),
        ]:
            ttk.Button(buttons, text=text, command=command, width=12).pack(fill="x", pady=2)

        out_frame = ttk.Frame(self)
        out_frame.grid(row=2, column=0, columnspan=2, sticky="ew")
        out_frame.columnconfigure(1, weight=1)
        ttk.Label(out_frame, text="Output file:").grid(row=0, column=0, sticky="w")
        ttk.Entry(out_frame, textvariable=self.output_var).grid(
            row=0, column=1, sticky="ew", padx=6)
        ttk.Button(out_frame, text="Browse…", command=self.choose_output).grid(row=0, column=2)

        ttk.Button(self, text="Combine", command=self.combine).grid(
            row=3, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Label(self, textvariable=self.status_var, foreground="#555").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))

    def files(self):
        return list(self.file_list.get(0, tk.END))

    def add_files(self):
        paths = filedialog.askopenfilenames(title="Choose CSV files", filetypes=CSV_TYPES,
                                            initialdir=self.last_dir)
        existing = set(self.files())
        for path in paths:
            if path not in existing:
                self.file_list.insert(tk.END, path)
                existing.add(path)
        if paths:
            self.last_dir = os.path.dirname(paths[0])
            if not self.output_var.get():
                self.output_var.set(os.path.join(self.last_dir, "combined.csv"))
        self.status_var.set(f"{self.file_list.size()} file(s) selected.")

    def remove_selected(self):
        for index in reversed(self.file_list.curselection()):
            self.file_list.delete(index)
        self.status_var.set(f"{self.file_list.size()} file(s) selected.")

    def move_selected(self, step):
        selected = list(self.file_list.curselection())
        if not selected:
            return
        size = self.file_list.size()
        if (step < 0 and selected[0] == 0) or (step > 0 and selected[-1] == size - 1):
            return
        items = self.files()
        order = selected if step < 0 else reversed(selected)
        for i in order:
            items[i], items[i + step] = items[i + step], items[i]
        self.file_list.delete(0, tk.END)
        for item in items:
            self.file_list.insert(tk.END, item)
        for i in selected:
            self.file_list.selection_set(i + step)

    def clear_files(self):
        self.file_list.delete(0, tk.END)
        self.status_var.set("Add the CSV files you want to join.")

    def choose_output(self):
        path = filedialog.asksaveasfilename(title="Save combined CSV as",
                                            defaultextension=".csv", filetypes=CSV_TYPES,
                                            initialdir=self.last_dir,
                                            initialfile="combined.csv")
        if path:
            self.output_var.set(path)

    def combine(self):
        inputs = self.files()
        output = self.output_var.get().strip()
        if not inputs:
            messagebox.showwarning("Nothing to join", "Add at least one CSV file first.")
            return
        if not output:
            messagebox.showwarning("No output file", "Choose where to save the combined CSV.")
            return
        missing = [f for f in inputs if not os.path.exists(f)]
        if missing:
            messagebox.showerror("Missing files", "These files no longer exist:\n\n" + "\n".join(missing))
            return
        out_abs = os.path.abspath(output)
        if any(os.path.abspath(f) == out_abs for f in inputs):
            messagebox.showerror("Output is an input",
                                 "The output file is also one of the input files and would be overwritten.")
            return

        try:
            mismatched = header_mismatches(inputs)
        except StopIteration:
            messagebox.showerror("Empty file", "One of the input files is empty (no header row).")
            return
        except (OSError, UnicodeDecodeError) as exc:
            messagebox.showerror("Could not read files", str(exc))
            return
        if mismatched:
            first_name = os.path.basename(inputs[0])
            details = "\n".join(f"• {os.path.basename(f)}: {', '.join(h)}" for f, h in mismatched)
            if not messagebox.askyesno(
                    "Headers don't match",
                    f"These files have different headers from {first_name}\n"
                    f"({', '.join(read_header(inputs[0]))}):\n\n{details}\n\n"
                    "Their header rows will be dropped and their columns may not line up.\n"
                    "Combine anyway?",
                    icon=messagebox.WARNING):
                self.status_var.set("Cancelled: headers don't match.")
                return

        try:
            combine_csv_files(inputs, output)
        except StopIteration:
            messagebox.showerror("Empty file", "One of the input files is empty (no header row).")
            return
        except (OSError, UnicodeDecodeError) as exc:
            messagebox.showerror("Could not combine files", str(exc))
            return

        self.status_var.set(f"Combined {len(inputs)} file(s) into {output}")
        messagebox.showinfo("Done", f"Combined {len(inputs)} file(s) into:\n{output}")


def main():
    root = tk.Tk()
    root.title("Join CSV Files")
    root.minsize(560, 360)
    app = JoinCsvApp(root)
    app.pack(fill="both", expand=True)
    root.mainloop()


if __name__ == "__main__":
    main()
