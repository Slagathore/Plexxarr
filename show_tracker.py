# =============================================================================
# show_tracker.py
# =============================================================================
# Radarr/Sonarr-style tracking logic, built on the same per-type tracker stack
# the request pipeline uses (Cole's requirement — TVDB→TMDB for TV, Jikan/MAL
# for anime and xanime; AniDB is identification-only, it has no cheap episode
# API):
#
#   scan_library_folders()  — walk typed tv/anime/xanime roots, identify each
#                             show folder against its tracker, map folders to
#                             shows (multiple folders per show = seasons split
#                             across drives, fully supported).
#   sync_show() / sync_all() — pull authoritative episode lists + air dates,
#                             then re-scan mapped folders to mark which
#                             episodes exist on disk.
#
# Missing = aired episodes without a file. Upcoming = air dates in the next
# N days. Both are plain queries in shows_store.
# =============================================================================

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import config
import shows_store
import torrent_routing
from media_lookup import (
    EpisodeInfo, MediaResult,
    get_jikan_episodes, get_jikan_status,
    get_tmdb_tv_episodes, get_tmdb_tv_status,
    get_tvdb_episodes, get_tvdb_series_status,
    search_jikan_anime, search_tmdb_shows, search_tvdb_shows,
    title_similarity,
)
from torrent_routing import VIDEO_EXTENSIONS, parse_torrent_name

logger = logging.getLogger(__name__)

_IDENTIFY_THRESHOLD = 0.75

# Folder names like "Show Name (2023)" — year improves tracker matching.
_FOLDER_YEAR_RE = re.compile(r"^(?P<name>.+?)\s*\((?P<year>(19|20)\d{2})\)\s*$")

# Anime fallback: files numbered without SxxEyy ("Show - 12 [1080p].mkv").
# Take the last standalone 1-4 digit number that isn't a year/resolution.
_ABS_EP_RE = re.compile(r"(?:^|[\s._-])(\d{1,4})(?=[\s._-]|$)")
_NOT_EPISODE = re.compile(r"^(?:19|20)\d{2}$|^(?:480|720|1080|2160)$")


@dataclass(frozen=True)
class ScanResult:
    identified: int
    already_tracked: int
    unidentified: list[str]


def _typed_roots(media_type: str) -> list[Path]:
    """Roots explicitly tagged with this type (mixed roots are skipped — a
    mixed folder can contain movies, and misidentifying those as shows would
    poison the inventory)."""
    return [
        Path(p.path) for p in config.MEDIA_LIBRARY_PATHS
        if p.media_type == media_type and Path(p.path).is_dir()
    ]


def _identify_folder(folder_name: str, media_type: str) -> MediaResult | None:
    name = folder_name
    year: int | None = None
    m = _FOLDER_YEAR_RE.match(folder_name)
    if m:
        name = m.group("name")
        year = int(m.group("year"))

    if media_type == "tv":
        results = search_tvdb_shows(name, year) or search_tmdb_shows(name, year)
    elif media_type == "anime":
        results = search_jikan_anime(name, explicit=False)
    elif media_type == "xanime":
        # Jikan (rating=rx) gives us MAL ids that can also sync episodes;
        # AniDB stays a request-pipeline identification source only.
        results = search_jikan_anime(name, explicit=True)
    else:
        return None

    best: MediaResult | None = None
    best_score = 0.0
    for r in results:
        score = title_similarity(name, r.title)
        if year and r.year and year == r.year:
            score += 0.1
        if score > best_score:
            best, best_score = r, score
    return best if best is not None and best_score >= _IDENTIFY_THRESHOLD else None


def scan_library_folders(media_types: tuple[str, ...] = ("tv", "anime", "xanime")) -> ScanResult:
    """Identify and track every unmapped show folder under the typed roots."""
    shows_store.initialize_shows_db()
    identified = already = 0
    unidentified: list[str] = []

    for media_type in media_types:
        for root in _typed_roots(media_type):
            try:
                subdirs = sorted(d for d in root.iterdir() if d.is_dir())
            except OSError as exc:
                logger.warning("Cannot scan %s: %s", root, exc)
                continue
            for folder in subdirs:
                if shows_store.folder_mapped(str(folder)):
                    already += 1
                    continue
                match = _identify_folder(folder.name, media_type)
                if match is None:
                    unidentified.append(str(folder))
                    logger.info("Could not identify show folder: %s", folder)
                    continue
                show_id = shows_store.upsert_show(
                    title=match.title, media_type=media_type,
                    source=match.source, external_id=match.external_id,
                    external_url=match.external_url or None, year=match.year,
                )
                shows_store.add_show_folder(show_id, str(folder))
                identified += 1
                logger.info(
                    "Tracked '%s' (%s:%s) ← %s",
                    match.title, match.source, match.external_id, folder,
                )

    return ScanResult(identified=identified, already_tracked=already,
                      unidentified=unidentified)


# ---------------------------------------------------------------------------
# Episode sync
# ---------------------------------------------------------------------------

# source → (episode fetcher, status fetcher). Tests monkeypatch this dict.
EPISODE_FETCHERS: dict[str, tuple] = {
    "tvdb": (get_tvdb_episodes, get_tvdb_series_status),
    "tmdb": (get_tmdb_tv_episodes, get_tmdb_tv_status),
    "jikan": (get_jikan_episodes, get_jikan_status),
}


def _parse_episode_from_file(name: str) -> tuple[int, int] | None:
    """(season, episode) from a filename; absolute-numbered anime → season 1."""
    parsed = parse_torrent_name(name)
    if parsed.episode is not None:
        return (parsed.season or 1, parsed.episode)

    # Fallback: strip bracket groups, then take the LAST plausible number.
    cleaned = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", Path(name).stem)
    candidates = [
        tok for tok in _ABS_EP_RE.findall(cleaned)
        if not _NOT_EPISODE.match(tok)
    ]
    if candidates:
        return (1, int(candidates[-1]))
    return None


def _scan_folders_for_episodes(folders: tuple[str, ...]) -> dict[tuple[int, int], str]:
    found: dict[tuple[int, int], str] = {}
    for folder in folders:
        root = Path(folder)
        if not root.is_dir():
            continue
        for f in root.rglob("*"):
            if not f.is_file() or f.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            key = _parse_episode_from_file(f.name)
            if key is not None:
                found.setdefault(key, str(f))
    return found


def sync_show(show_id: int) -> str:
    """Refresh one show's episode list + on-disk state. Returns a summary."""
    show = shows_store.get_show(show_id)
    if show is None:
        return f"show #{show_id} not found"

    fetchers = EPISODE_FETCHERS.get(show.source)
    if fetchers is None:
        # AniDB-identified or unknown source: no episode API — disk state only.
        found = _scan_folders_for_episodes(show.folders)
        shows_store.update_file_state(show_id, found)
        return f"{show.title}: no episode source ({show.source}); disk state only ({len(found)} files)"

    fetch_episodes, fetch_status = fetchers
    episodes: list[EpisodeInfo] = fetch_episodes(show.external_id)
    if episodes:
        shows_store.replace_episodes(show_id, episodes)
    shows_store.set_show_status(show_id, fetch_status(show.external_id))

    found = _scan_folders_for_episodes(show.folders)
    shows_store.update_file_state(show_id, found)
    missing = len(shows_store.missing_episodes(show_id))
    return f"{show.title}: {len(episodes)} episodes known, {len(found)} on disk, {missing} missing"


def sync_all() -> list[str]:
    return [sync_show(s.show_id) for s in shows_store.list_shows()]


# ---------------------------------------------------------------------------
# Deterministic routing for tracked episodes (feeds the download pipeline)
# ---------------------------------------------------------------------------

def _folder_containing_season(show: shows_store.TrackedShow, season: int) -> str | None:
    """The mapped folder that already holds this season's subfolder, if any."""
    season_re = re.compile(rf"^(?:Season[\s._-]*0*{season}|S0*{season})$", re.IGNORECASE)
    for folder in show.folders:
        root = Path(folder)
        if not root.is_dir():
            continue
        try:
            if any(d.is_dir() and season_re.match(d.name.strip()) for d in root.iterdir()):
                return folder
        except OSError:
            continue
    return None


def plan_for_episode(
    show: shows_store.TrackedShow, season: int, episode: int,
) -> torrent_routing.RoutePlan:
    """Route plan for a KNOWN episode of a tracked show — no fuzzy matching.

    Precedence for the destination:
      1. An explicit per-season target folder (season_targets) — the file
         lands directly in that folder.
      2. The mapped folder that already contains this season, keeping its
         season-subfolder naming style.
      3. The show's first mapped folder, creating "Season NN" per the
         sibling style.
    Only a show with no mapped folders at all falls back to staging.
    """
    new_filename = torrent_routing.sanitize_for_filesystem(
        f"{show.title} - S{season:02d}E{episode:02d}"
    )

    target = shows_store.get_season_target(show.show_id, season)
    if target:
        return torrent_routing.RoutePlan(
            confident=True, dest_dir=target, new_filename=new_filename,
            season_folder=None, show_folder=None,
            reason=f"season target rule for '{show.title}' S{season}",
        )

    folder = _folder_containing_season(show, season) or (
        show.folders[0] if show.folders else None
    )
    if folder is None:
        return torrent_routing.RoutePlan(
            confident=False, dest_dir=str(Path(config.TORRENT_DOWNLOAD_DIR)),
            reason=f"'{show.title}' has no mapped folders — staying in staging",
        )

    season_name = torrent_routing._season_folder_name(Path(folder), season)
    return torrent_routing.RoutePlan(
        confident=True,
        dest_dir=str(Path(folder) / season_name),
        new_filename=new_filename,
        show_folder=folder, season_folder=season_name,
        reason=f"tracked show '{show.title}' → {Path(folder).name}/{season_name}",
    )
