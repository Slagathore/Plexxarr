# =============================================================================
# ui_helpers.py
# =============================================================================
# Small reusable Tk helpers shared by the desktop tabs:
#
#   make_sortable(tree)  — click a Treeview column header to sort by it
#                          (click again to reverse). Numeric-aware.
#   Spinner              — animated "working…" indicator label (braille
#                          frames, no image asset needed).
# =============================================================================

import re
import tkinter as tk
from tkinter import ttk

_NUMERIC_RE = re.compile(r"^\s*-?\d+(?:\.\d+)?\s*$")
_FRACTION_RE = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*$")  # "123/321" have-counts


def _sort_key(value: str):
    """Key that sorts numbers numerically and everything else case-folded."""
    m = _FRACTION_RE.match(value)
    if m:
        return (0, int(m.group(1)))
    if _NUMERIC_RE.match(value):
        return (0, float(value))
    return (1, value.casefold())


def make_sortable(tree: ttk.Treeview, *, on_sorted=None) -> None:
    """Make every column of a Treeview sortable by clicking its header.

    Sorting reorders the existing rows in place (iids and values are kept),
    so callers that map iids back to data keep working. on_sorted() fires
    after each re-order, for callers that track row order separately.
    """
    state = {"column": None, "reverse": False}
    columns = tree["columns"]

    def sort_by(col: str) -> None:
        reverse = state["column"] == col and not state["reverse"]
        state["column"], state["reverse"] = col, reverse
        rows = [(tree.set(iid, col), iid) for iid in tree.get_children("")]
        rows.sort(key=lambda pair: _sort_key(pair[0]), reverse=reverse)
        for index, (_val, iid) in enumerate(rows):
            tree.move(iid, "", index)
        # Arrow on the active header only.
        for c in columns:
            base = tree.heading(c, "text").rstrip(" ▲▼")
            suffix = ""
            if c == col:
                suffix = " ▼" if reverse else " ▲"
            tree.heading(c, text=base + suffix)
        if on_sorted is not None:
            on_sorted()

    for col in columns:
        # Default arg binds the current column name.
        tree.heading(col, command=lambda c=col: sort_by(c))


class Spinner:
    """Animated text spinner — the "loading gif" for long operations.

    Attach to any ttk.Label; start() animates braille frames next to the
    given message, stop() clears it. Runs on the Tk after() loop, so it must
    be started/stopped from the UI thread.
    """

    _FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, label: ttk.Label) -> None:
        self._label = label
        self._running = False
        self._frame = 0
        self._text = ""

    def start(self, text: str = "Working…") -> None:
        self._text = text
        if not self._running:
            self._running = True
            self._tick()

    def update_text(self, text: str) -> None:
        self._text = text

    def stop(self, final_text: str = "") -> None:
        self._running = False
        self._label.configure(text=final_text)

    def _tick(self) -> None:
        if not self._running:
            return
        frame = self._FRAMES[self._frame % len(self._FRAMES)]
        self._frame += 1
        self._label.configure(text=f"{frame} {self._text}")
        try:
            self._label.after(120, self._tick)
        except tk.TclError:
            self._running = False
