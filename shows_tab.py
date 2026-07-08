# =============================================================================
# shows_tab.py
# =============================================================================
# The "Shows" notebook tab — Radarr/Sonarr-style tracking UI. This is the
# first tab extracted out of the DesktopApp god-class into its own module:
# it owns its widgets and talks back to the app through a narrow surface
# (_post_to_ui, _show_warning, open_downloads_search).
#
# Layout (vertical PanedWindow — every section is drag-resizable):
#   toolbar  — Scan / Sync / Refresh / Untrack / Merge / Silenced… / Fix
#              Titles + auto-grab toggle + animated progress spinner
#   upcoming — one box per air date (next 14 days); each box lists what
#              releases that day and right-click offers Silence /
#              Auto-download-on-release / Find torrent
#   shows    — inventory tree: search box + per-type filter checkboxes,
#              click a column header to sort
#   missing  — missing episodes for the selected show
# =============================================================================

import logging
import threading
import time
import tkinter as tk
from tkinter import filedialog, ttk

import config
import show_tracker
import shows_store
from ui_helpers import Spinner, make_sortable

logger = logging.getLogger(__name__)


class ShowsTab:
    def __init__(self, parent: ttk.Frame, app) -> None:
        self.app = app  # DesktopApp — narrow surface only (see module docstring)
        self._shows: list[shows_store.TrackedShow] = []
        self._missing: list[shows_store.EpisodeRow] = []
        self._status_var = tk.StringVar(value="Scan Folders to identify your show libraries.")
        self._search_var = tk.StringVar()
        # Only one scan/sync/grab may run at a time — see _run_guarded. This
        # flag is touched only on the Tk main thread, so no lock is needed.
        self._busy = False
        # (show_id, listbox-entry metadata) per upcoming box, for the
        # right-click menu.
        self._upcoming_boxes: list[tuple[tk.Listbox, list[tuple[int, str]]]] = []

        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)

        toolbar = ttk.Frame(parent)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Button(toolbar, text="Scan Folders", command=self.scan_folders).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(toolbar, text="Sync Episodes", command=self.sync_all).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(toolbar, text="Refresh", command=self.refresh).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(toolbar, text="Untrack", command=self.untrack_selected).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(toolbar, text="Merge Selected", command=self.merge_selected).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(toolbar, text="Silenced…", command=self.open_silenced_dialog).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(toolbar, text="Fix Titles (English)", command=self.fix_titles).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(toolbar, text="⬇ Grab Missing Now", command=self.grab_missing_now).pack(side=tk.LEFT, padx=(0, 6))
        self._auto_grab_var = tk.BooleanVar(value=config.SHOWS_AUTO_GRAB)
        ttk.Checkbutton(
            toolbar, text="Auto-grab missing", variable=self._auto_grab_var,
            command=lambda: self.app._persist_dl_toggle("SHOWS_AUTO_GRAB", self._auto_grab_var),
        ).pack(side=tk.LEFT, padx=(0, 6))
        spinner_label = ttk.Label(toolbar, text="", font=("Segoe UI", 9))
        spinner_label.pack(side=tk.LEFT, padx=(10, 0))
        self._spinner = Spinner(spinner_label)
        ttk.Label(toolbar, textvariable=self._status_var,
                  font=("Segoe UI", 9, "italic")).pack(side=tk.LEFT, padx=(10, 0))

        panes = ttk.PanedWindow(parent, orient=tk.VERTICAL)
        panes.grid(row=1, column=0, sticky="nsew")

        # ------------------------------------------------------------------
        # Upcoming — per-date boxes on a horizontally scrollable strip
        # ------------------------------------------------------------------
        upcoming_frame = ttk.LabelFrame(panes, text="Upcoming (next 14 days) — right-click an entry to silence or auto-download", padding=6)
        panes.add(upcoming_frame, weight=1)
        upcoming_frame.columnconfigure(0, weight=1)
        upcoming_frame.rowconfigure(0, weight=1)

        self._upcoming_canvas = tk.Canvas(upcoming_frame, height=150, highlightthickness=0)
        self._upcoming_canvas.grid(row=0, column=0, sticky="nsew")
        up_scroll = ttk.Scrollbar(upcoming_frame, orient=tk.HORIZONTAL,
                                  command=self._upcoming_canvas.xview)
        up_scroll.grid(row=1, column=0, sticky="ew")
        self._upcoming_canvas.configure(xscrollcommand=up_scroll.set)
        self._upcoming_inner = ttk.Frame(self._upcoming_canvas)
        self._upcoming_window = self._upcoming_canvas.create_window(
            (0, 0), window=self._upcoming_inner, anchor="nw")
        self._upcoming_inner.bind(
            "<Configure>",
            lambda _e: self._upcoming_canvas.configure(
                scrollregion=self._upcoming_canvas.bbox("all")),
        )

        # ------------------------------------------------------------------
        # Tracked shows — search + type filter + sortable tree
        # ------------------------------------------------------------------
        shows_frame = ttk.LabelFrame(panes, text="Tracked shows", padding=6)
        panes.add(shows_frame, weight=3)
        shows_frame.columnconfigure(0, weight=1)
        shows_frame.rowconfigure(1, weight=1)

        filter_row = ttk.Frame(shows_frame)
        filter_row.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(filter_row, text="Search:").pack(side=tk.LEFT)
        search_entry = ttk.Entry(filter_row, textvariable=self._search_var, width=28)
        search_entry.pack(side=tk.LEFT, padx=(4, 12))
        search_entry.bind("<KeyRelease>", lambda _e: self._render_shows())
        ttk.Label(filter_row, text="Type:").pack(side=tk.LEFT)
        self._type_filter_vars: dict[str, tk.BooleanVar] = {}
        for tag, label in (("tv", "TV"), ("anime", "Anime"), ("xanime", "xAnime")):
            var = tk.BooleanVar(value=True)
            self._type_filter_vars[tag] = var
            ttk.Checkbutton(filter_row, text=label, variable=var,
                            command=self._render_shows).pack(side=tk.LEFT, padx=4)

        shows_tree_frame = ttk.Frame(shows_frame)
        shows_tree_frame.grid(row=1, column=0, sticky="nsew")
        shows_tree_frame.columnconfigure(0, weight=1)
        shows_tree_frame.rowconfigure(0, weight=1)
        shows = ttk.Treeview(
            shows_tree_frame,
            columns=("id", "title", "type", "status", "have", "missing", "next", "flags", "folders"),
            show="headings", selectmode="extended",
        )
        for col, text, width, stretch in (
            ("id", "#", 40, False), ("title", "Title", 260, True),
            ("type", "Type", 60, False), ("status", "Status", 100, False),
            ("have", "Have", 70, False), ("missing", "Missing", 60, False),
            ("next", "Next air", 90, False), ("flags", "Flags", 60, False),
            ("folders", "Folders", 240, True),
        ):
            shows.heading(col, text=text)
            shows.column(col, width=width, anchor=tk.W, stretch=stretch)
        shows.grid(row=0, column=0, sticky="nsew")
        shows_scroll = ttk.Scrollbar(shows_tree_frame, orient=tk.VERTICAL, command=shows.yview)
        shows_scroll.grid(row=0, column=1, sticky="ns")
        shows.configure(yscrollcommand=shows_scroll.set)
        shows.bind("<<TreeviewSelect>>", lambda _e: self._show_selected_missing())
        make_sortable(shows)
        self._shows_tree = shows

        # ------------------------------------------------------------------
        # Missing episodes
        # ------------------------------------------------------------------
        missing_frame = ttk.LabelFrame(panes, text="Missing episodes (selected show)", padding=6)
        panes.add(missing_frame, weight=2)
        missing_frame.columnconfigure(0, weight=1)
        missing_frame.rowconfigure(1, weight=1)

        missing_bar = ttk.Frame(missing_frame)
        missing_bar.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ttk.Button(missing_bar, text="Sync Selected Show",
                   command=self.sync_selected).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(missing_bar, text="Set Season Target Folder…",
                   command=self.set_season_target_for_selected).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(missing_bar, text="⬇ Find Torrent for Selected Episode",
                   command=self.find_torrent_for_selected_episode).pack(side=tk.RIGHT)

        missing_tree_frame = ttk.Frame(missing_frame)
        missing_tree_frame.grid(row=1, column=0, sticky="nsew")
        missing_tree_frame.columnconfigure(0, weight=1)
        missing_tree_frame.rowconfigure(0, weight=1)
        missing = ttk.Treeview(
            missing_tree_frame, columns=("ep", "title", "aired"),
            show="headings", selectmode="browse",
        )
        for col, text, width, stretch in (
            ("ep", "Episode", 90, False), ("title", "Title", 380, True),
            ("aired", "Aired", 100, False),
        ):
            missing.heading(col, text=text)
            missing.column(col, width=width, anchor=tk.W, stretch=stretch)
        missing.grid(row=0, column=0, sticky="nsew")
        missing_scroll = ttk.Scrollbar(missing_tree_frame, orient=tk.VERTICAL, command=missing.yview)
        missing_scroll.grid(row=0, column=1, sticky="ns")
        missing.configure(yscrollcommand=missing_scroll.set)
        make_sortable(missing)
        self._missing_tree = missing

    # ------------------------------------------------------------------
    # Data refresh
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        self._shows = shows_store.list_shows()
        self._render_shows()
        self._render_upcoming()
        self._show_selected_missing()

    def _render_shows(self) -> None:
        selected = set(self._selected_show_ids())
        query = self._search_var.get().strip().casefold()
        active_types = {tag for tag, var in self._type_filter_vars.items() if var.get()}

        for item in self._shows_tree.get_children():
            self._shows_tree.delete(item)
        for s in self._shows:
            if s.media_type in self._type_filter_vars and s.media_type not in active_types:
                continue
            if query and query not in s.title.casefold():
                continue
            folders = "; ".join(s.folders) if s.folders else "—"
            flags = ("🔕" if s.silenced else "") + ("⬇" if s.auto_grab else "")
            self._shows_tree.insert(
                "", "end", iid=str(s.show_id),
                values=(s.show_id, s.title, s.media_type, s.status or "?",
                        f"{s.have_count}/{s.episode_count}", s.missing_count,
                        s.next_air_date or "", flags, folders),
            )
            if s.show_id in selected:
                self._shows_tree.selection_add(str(s.show_id))

    def _render_upcoming(self) -> None:
        for child in self._upcoming_inner.winfo_children():
            child.destroy()
        self._upcoming_boxes = []

        by_date: dict[str, list[tuple[shows_store.TrackedShow, shows_store.EpisodeRow]]] = {}
        for show, ep in shows_store.upcoming_episodes(days=14):
            by_date.setdefault(ep.air_date or "?", []).append((show, ep))

        if not by_date:
            ttk.Label(self._upcoming_inner,
                      text="Nothing airing in the next 14 days (or nothing synced yet).",
                      font=("Segoe UI", 9, "italic")).grid(row=0, column=0, padx=8, pady=8)
            return

        for col, date_str in enumerate(sorted(by_date)):
            box = ttk.LabelFrame(self._upcoming_inner, text=date_str, padding=4)
            box.grid(row=0, column=col, sticky="ns", padx=(0, 8), pady=(0, 4))
            entries = by_date[date_str]
            listbox = tk.Listbox(box, width=34, height=min(max(len(entries), 3), 6),
                                 activestyle="none", exportselection=False)
            listbox.grid(row=0, column=0, sticky="nsew")
            if len(entries) > 6:
                sb = ttk.Scrollbar(box, orient=tk.VERTICAL, command=listbox.yview)
                sb.grid(row=0, column=1, sticky="ns")
                listbox.configure(yscrollcommand=sb.set)
            meta: list[tuple[int, str]] = []
            for show, ep in entries:
                marker = "⬇ " if show.auto_grab else ""
                have = " ✓" if ep.has_file else ""
                listbox.insert(tk.END, f"{marker}{show.title}  S{ep.season:02d}E{ep.episode:02d}{have}")
                meta.append((show.show_id, f"{show.title} S{ep.season:02d}E{ep.episode:02d}"))
            listbox.bind("<Button-3>", self._on_upcoming_right_click)
            self._upcoming_boxes.append((listbox, meta))
        if hasattr(self.app, "_apply_dark_widget_styles"):
            self.app._apply_dark_widget_styles(self._upcoming_inner)

    def _on_upcoming_right_click(self, event) -> None:
        listbox = event.widget
        meta = next((m for lb, m in self._upcoming_boxes if lb is listbox), None)
        if meta is None:
            return
        index = listbox.nearest(event.y)
        if index < 0 or index >= len(meta):
            return
        listbox.selection_clear(0, tk.END)
        listbox.selection_set(index)
        show_id, query = meta[index]
        show = shows_store.get_show(show_id)
        if show is None:
            return

        menu = tk.Menu(listbox, tearoff=0)
        menu.add_command(
            label=f"🔕 Silence releases of '{show.title}'",
            command=lambda: (shows_store.set_show_silenced(show_id, True), self.refresh()),
        )
        auto_label = ("Stop auto-downloading on release" if show.auto_grab
                      else "⬇ Auto-download when it releases")
        menu.add_command(
            label=auto_label,
            command=lambda: (shows_store.set_show_auto_grab(show_id, not show.auto_grab),
                             self.refresh()),
        )
        menu.add_separator()
        menu.add_command(
            label="Find torrent now…",
            command=lambda: self.app.open_downloads_search(query, show.media_type),
        )
        menu.tk_popup(event.x_root, event.y_root)

    def open_silenced_dialog(self) -> None:
        """List silenced shows with a Restore button each."""
        silenced = [s for s in shows_store.list_shows() if s.silenced]
        win = tk.Toplevel(self._shows_tree)
        win.title("Silenced shows")
        win.geometry("420x360")
        win.transient(self._shows_tree.winfo_toplevel())
        win.columnconfigure(0, weight=1)
        win.rowconfigure(0, weight=1)

        listbox = tk.Listbox(win, activestyle="none")
        listbox.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        for s in silenced:
            listbox.insert(tk.END, f"{s.title}  ({s.media_type})")
        if not silenced:
            listbox.insert(tk.END, "No silenced shows.")

        def restore() -> None:
            sel = listbox.curselection()
            if not sel or not silenced:
                return
            idx = sel[0]
            if idx >= len(silenced):
                return
            shows_store.set_show_silenced(silenced[idx].show_id, False)
            listbox.delete(idx)
            del silenced[idx]
            self.refresh()

        bar = ttk.Frame(win)
        bar.grid(row=1, column=0, sticky="e", padx=8, pady=(0, 8))
        ttk.Button(bar, text="Restore Selected", command=restore).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(bar, text="Close", command=win.destroy).pack(side=tk.RIGHT)
        if hasattr(self.app, "_apply_dark_widget_styles"):
            self.app._apply_dark_widget_styles(win)

    def _selected_show_ids(self) -> list[int]:
        return [int(iid) for iid in self._shows_tree.selection()]

    def _selected_show_id(self) -> int | None:
        ids = self._selected_show_ids()
        return ids[0] if ids else None

    def _show_selected_missing(self) -> None:
        for item in self._missing_tree.get_children():
            self._missing_tree.delete(item)
        show_id = self._selected_show_id()
        self._missing = []
        if show_id is None:
            return
        self._missing = shows_store.missing_episodes(show_id)
        for idx, ep in enumerate(self._missing):
            self._missing_tree.insert(
                "", "end", iid=str(idx),
                values=(f"S{ep.season:02d}E{ep.episode:02d}", ep.title, ep.air_date or ""),
            )

    # ------------------------------------------------------------------
    # Actions (workers post back via app._post_to_ui)
    # ------------------------------------------------------------------

    def _run_guarded(self, name: str, thread_name: str, running_msg: str,
                     work, describe) -> None:
        """Run a scan/sync/grab in a worker thread, at most one at a time.

        Blocks a second click while one is in flight (the reported bug: 4
        clicks = 4 concurrent scans hammering Jikan). describe(result) -> str
        builds the done message; ShowsBusyError (from the module-level guard,
        e.g. the scheduler beat us to it) is reported plainly, not as a crash.
        """
        if self._busy:
            self._status_var.set(f"Already running — '{name}' skipped until it finishes.")
            return
        self._busy = True
        self._status_var.set("")
        self._spinner.start(running_msg)

        def worker() -> None:
            try:
                msg = describe(work())
            except show_tracker.ShowsBusyError as exc:
                msg = str(exc)
            except Exception as exc:
                logger.exception("%s failed.", name)
                msg = f"{name} failed: {exc}"
            self.app._post_to_ui(lambda: self._finish_operation(msg))

        threading.Thread(target=worker, name=thread_name, daemon=True).start()

    def _finish_operation(self, msg: str) -> None:
        self._busy = False
        self._spinner.stop()
        self._status_var.set(msg)
        self.refresh()

    def scan_folders(self) -> None:
        def describe(result) -> str:
            msg = (f"Identified {result.identified} new show(s); "
                   f"{result.already_tracked} already tracked; "
                   f"{len(result.unidentified)} unidentified")
            return msg + (" (see log for folder names)" if result.unidentified else "")

        self._run_guarded(
            "Scan Folders", "shows-scan",
            "Scanning library folders and identifying shows…",
            show_tracker.scan_library_folders, describe,
        )

    def sync_all(self) -> None:
        started_at = time.time()

        def progress(done: int, total: int, title: str) -> None:
            # Called from the worker thread — estimate remaining time from
            # the average per-show pace so far, then hop to the UI thread.
            if done > 0:
                per_show = (time.time() - started_at) / done
                remaining = int(per_show * (total - done))
                eta = f" — ~{remaining // 60}m {remaining % 60:02d}s left"
            else:
                eta = ""
            text = f"Syncing {done + 1}/{total}: {title[:40]}{eta}"
            self.app._post_to_ui(lambda: self._spinner.update_text(text))

        self._run_guarded(
            "Sync Episodes", "shows-sync",
            "Syncing episode lists for all tracked shows…",
            lambda: show_tracker.sync_all(progress=progress),
            lambda summaries: f"Synced {len(summaries)} show(s)",
        )

    def sync_selected(self) -> None:
        show_id = self._selected_show_id()
        if show_id is None:
            self.app._show_warning("No show selected", "Select a show first.")
            return
        self._run_guarded(
            "Sync show", "shows-sync-one", "Syncing…",
            lambda: show_tracker.sync_show(show_id), lambda msg: msg,
        )

    def fix_titles(self) -> None:
        """Rename AniDB-identified shows to their official English titles."""
        self._run_guarded(
            "Fix Titles", "shows-fix-titles",
            "Looking up English titles in the AniDB dump…",
            show_tracker.backfill_english_titles,
            lambda n: f"Renamed {n} show(s) to their English title",
        )

    def untrack_selected(self) -> None:
        ids = self._selected_show_ids()
        if not ids:
            self.app._show_warning("No show selected", "Select a show first.")
            return
        if not self.app._ask_yes_no(
            "Untrack show",
            f"Stop tracking {len(ids)} show(s)? (No files are touched — this "
            "only removes them from the tracker.)",
        ):
            return
        for show_id in ids:
            shows_store.remove_show(show_id)
        self.refresh()

    def merge_selected(self) -> None:
        """Merge duplicate rows: Ctrl-click the rows that are the same show."""
        ids = self._selected_show_ids()
        if len(ids) < 2:
            self.app._show_warning(
                "Merge shows",
                "Ctrl-click two or more rows that are actually the same show, "
                "then click Merge Selected. The FIRST row (top-most) is kept.",
            )
            return
        by_id = {s.show_id: s for s in self._shows}
        titles = [by_id[i].title for i in ids if i in by_id]
        primary = ids[0]
        if not self.app._ask_yes_no(
            "Merge shows",
            "Merge these into one tracked show?\n\n  • " + "\n  • ".join(titles)
            + f"\n\nKeeping: {by_id[primary].title if primary in by_id else primary}. "
            "Folders and on-disk episodes move to it; the other rows are removed.",
        ):
            return
        merged = shows_store.merge_shows(primary, ids[1:])
        self._status_var.set(f"Merged {merged} duplicate(s) into '{by_id[primary].title}'")
        self.refresh()

    def grab_missing_now(self) -> None:
        """Run one auto-grab pass immediately (rename+move forced on)."""
        self._run_guarded(
            "Grab Missing", "shows-grab-missing",
            "Searching torrents for missing episodes…",
            self.app.download_manager.auto_grab_missing_episodes,
            lambda started: (
                f"Started {len(started)} download(s) — see the Downloads tab"
                if started else "No grabbable missing episodes found this pass"
            ),
        )

    def set_season_target_for_selected(self) -> None:
        """Pin a season of the selected show to an explicit folder (rule the
        torrent pipeline routes into — including a folder on another drive)."""
        show_id = self._selected_show_id()
        if show_id is None:
            self.app._show_warning("No show selected", "Select a show first.")
            return
        sel = self._missing_tree.selection()
        if sel:
            try:
                season = self._missing[int(sel[0])].season
            except (ValueError, IndexError):
                season = None
        else:
            season = None
        if season is None:
            # No missing episode selected — ask which season the rule is for.
            from tkinter import simpledialog
            season = simpledialog.askinteger(
                "Season target", "Season number for the target-folder rule:",
                minvalue=0, maxvalue=99,
            )
            if season is None:
                return

        current = shows_store.get_season_target(show_id, season)
        path = filedialog.askdirectory(
            title=f"Target folder for Season {season}",
            initialdir=current or None,
        )
        if not path:
            return
        shows_store.set_season_target(show_id, season, path)
        show = shows_store.get_show(show_id)
        self._status_var.set(
            f"Season {season} of '{show.title if show else show_id}' now routes to {path}"
        )

    def find_torrent_for_selected_episode(self) -> None:
        show_id = self._selected_show_id()
        sel = self._missing_tree.selection()
        if show_id is None or not sel:
            self.app._show_warning(
                "No episode selected",
                "Select a show, then one of its missing episodes.",
            )
            return
        show = shows_store.get_show(show_id)
        try:
            ep = self._missing[int(sel[0])]
        except (ValueError, IndexError):
            return
        if show is None:
            return
        query = f"{show.title} S{ep.season:02d}E{ep.episode:02d}"
        self.app.open_downloads_search(
            query, show.media_type,
            episode_context=(show.show_id, ep.season, ep.episode),
        )
