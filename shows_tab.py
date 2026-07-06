# =============================================================================
# shows_tab.py
# =============================================================================
# The "Shows" notebook tab — Radarr/Sonarr-style tracking UI. This is the
# first tab extracted out of the DesktopApp god-class into its own module:
# it owns its widgets and talks back to the app through a narrow surface
# (_post_to_ui, _show_warning, open_downloads_search).
#
# Layout:
#   toolbar  — Scan Folders / Sync Episodes / Refresh + status line
#   upcoming — episodes airing in the next 14 days across all tracked shows
#   shows    — inventory tree (have/missing counts, status, next air date)
#   missing  — missing episodes for the selected show, with a jump-to-
#              Downloads button to grab one
# =============================================================================

import logging
import threading
import tkinter as tk
from tkinter import filedialog, ttk

import config
import show_tracker
import shows_store

logger = logging.getLogger(__name__)


class ShowsTab:
    def __init__(self, parent: ttk.Frame, app) -> None:
        self.app = app  # DesktopApp — narrow surface only (see module docstring)
        self._shows: list[shows_store.TrackedShow] = []
        self._missing: list[shows_store.EpisodeRow] = []
        self._status_var = tk.StringVar(value="Scan Folders to identify your show libraries.")
        # Only one scan/sync/grab may run at a time — see _run_guarded. This
        # flag is touched only on the Tk main thread, so no lock is needed.
        self._busy = False

        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)
        parent.rowconfigure(4, weight=2)
        parent.rowconfigure(6, weight=1)

        toolbar = ttk.Frame(parent)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Button(toolbar, text="Scan Folders", command=self.scan_folders).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(toolbar, text="Sync Episodes", command=self.sync_all).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(toolbar, text="Refresh", command=self.refresh).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(toolbar, text="Untrack Selected", command=self.untrack_selected).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(toolbar, text="⬇ Grab Missing Now", command=self.grab_missing_now).pack(side=tk.LEFT, padx=(0, 6))
        self._auto_grab_var = tk.BooleanVar(value=config.SHOWS_AUTO_GRAB)
        ttk.Checkbutton(
            toolbar, text="Auto-grab missing", variable=self._auto_grab_var,
            command=lambda: self.app._persist_dl_toggle("SHOWS_AUTO_GRAB", self._auto_grab_var),
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Label(toolbar, textvariable=self._status_var,
                  font=("Segoe UI", 9, "italic")).pack(side=tk.LEFT, padx=(10, 0))

        ttk.Label(parent, text="Upcoming (next 14 days)",
                  font=("Segoe UI", 10, "bold")).grid(row=1, column=0, sticky="w", pady=(0, 4))
        upcoming = ttk.Treeview(
            parent, columns=("air", "show", "ep", "title", "have"),
            show="headings", height=4, selectmode="browse",
        )
        for col, text, width, stretch in (
            ("air", "Airs", 100, False), ("show", "Show", 260, True),
            ("ep", "Episode", 80, False), ("title", "Title", 300, True),
            ("have", "Have", 60, False),
        ):
            upcoming.heading(col, text=text)
            upcoming.column(col, width=width, anchor=tk.W, stretch=stretch)
        upcoming.grid(row=2, column=0, sticky="nsew")
        self._upcoming_tree = upcoming

        ttk.Label(parent, text="Tracked shows",
                  font=("Segoe UI", 10, "bold")).grid(row=3, column=0, sticky="w", pady=(8, 4))
        shows = ttk.Treeview(
            parent,
            columns=("id", "title", "type", "status", "have", "missing", "next", "folders"),
            show="headings", height=8, selectmode="browse",
        )
        for col, text, width, stretch in (
            ("id", "#", 40, False), ("title", "Title", 260, True),
            ("type", "Type", 60, False), ("status", "Status", 110, False),
            ("have", "Have", 70, False), ("missing", "Missing", 60, False),
            ("next", "Next air", 90, False), ("folders", "Folders", 260, True),
        ):
            shows.heading(col, text=text)
            shows.column(col, width=width, anchor=tk.W, stretch=stretch)
        shows.grid(row=4, column=0, sticky="nsew")
        shows.bind("<<TreeviewSelect>>", lambda _e: self._show_selected_missing())
        self._shows_tree = shows

        missing_bar = ttk.Frame(parent)
        missing_bar.grid(row=5, column=0, sticky="ew", pady=(8, 4))
        ttk.Label(missing_bar, text="Missing episodes (selected show)",
                  font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT)
        ttk.Button(missing_bar, text="Sync Selected Show",
                   command=self.sync_selected).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(missing_bar, text="Set Season Target Folder…",
                   command=self.set_season_target_for_selected).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(missing_bar, text="⬇ Find Torrent for Selected Episode",
                   command=self.find_torrent_for_selected_episode).pack(side=tk.RIGHT)

        missing = ttk.Treeview(
            parent, columns=("ep", "title", "aired"),
            show="headings", height=5, selectmode="browse",
        )
        for col, text, width, stretch in (
            ("ep", "Episode", 90, False), ("title", "Title", 380, True),
            ("aired", "Aired", 100, False),
        ):
            missing.heading(col, text=text)
            missing.column(col, width=width, anchor=tk.W, stretch=stretch)
        missing.grid(row=6, column=0, sticky="nsew")
        self._missing_tree = missing

    # ------------------------------------------------------------------
    # Data refresh
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        self._shows = shows_store.list_shows()
        selected = self._selected_show_id()

        for item in self._shows_tree.get_children():
            self._shows_tree.delete(item)
        for s in self._shows:
            folders = "; ".join(s.folders) if s.folders else "—"
            self._shows_tree.insert(
                "", "end", iid=str(s.show_id),
                values=(s.show_id, s.title, s.media_type, s.status or "?",
                        f"{s.have_count}/{s.episode_count}", s.missing_count,
                        s.next_air_date or "", folders),
            )
            if s.show_id == selected:
                self._shows_tree.selection_set(str(s.show_id))

        for item in self._upcoming_tree.get_children():
            self._upcoming_tree.delete(item)
        for show, ep in shows_store.upcoming_episodes(days=14):
            self._upcoming_tree.insert(
                "", "end",
                values=(ep.air_date, show.title, f"S{ep.season:02d}E{ep.episode:02d}",
                        ep.title, "✓" if ep.has_file else ""),
            )
        self._show_selected_missing()

    def _selected_show_id(self) -> int | None:
        sel = self._shows_tree.selection()
        return int(sel[0]) if sel else None

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
        self._status_var.set(running_msg)

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
        self._run_guarded(
            "Sync Episodes", "shows-sync",
            "Syncing episode lists for all tracked shows…",
            show_tracker.sync_all, lambda summaries: f"Synced {len(summaries)} show(s)",
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

    def untrack_selected(self) -> None:
        show_id = self._selected_show_id()
        if show_id is None:
            self.app._show_warning("No show selected", "Select a show first.")
            return
        if not self.app._ask_yes_no(
            "Untrack show",
            "Stop tracking this show? (No files are touched — this only "
            "removes it from the tracker.)",
        ):
            return
        shows_store.remove_show(show_id)
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
