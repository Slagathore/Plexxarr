# =============================================================================
# publish_anime_db.py
# =============================================================================
# Builds the weekly anime-metadata artifact that .github/workflows/anime-db.yml
# attaches to a GitHub Release (Task I / FIX_SPRINT_BOOTSTRAP.md section I).
#
# Contains ONLY manami-project/anime-offline-database tables (ODbL 1.0, with
# a NOTICE, share-alike). Fribb/anime-lists and Anime-Lists/anime-lists
# publish no license file at all — there is no permission to redistribute
# their id mappings — so this artifact never includes them. Every Sensarr
# client fetches those two small dumps directly from their own canonical
# raw.githubusercontent URLs and merges them locally after downloading this
# artifact (anime_db._merge_local_id_sources), exactly like the full local
# build always did for all three sources.
#
# Headless, no upload logic: the workflow does the `gh release` calls. This
# script only produces the four files a release needs in --output-dir:
#   anime-db.sqlite      (uncompressed, for local inspection — not uploaded)
#   anime-db.sqlite.gz   (the artifact clients actually download)
#   latest.json          (the manifest: schema_version, built, sha256, url, bytes)
#   NOTICE                (ODbL attribution — covers manami only, see above)
#
# Run: python publish_anime_db.py --output-dir dist-anime-db
# =============================================================================

import argparse
import gzip
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import anime_db

DEFAULT_TAG = anime_db._MANIFEST_TAG
DEFAULT_REPO = anime_db._REPO
ARTIFACT_NAME = "anime-db.sqlite"
GZ_NAME = ARTIFACT_NAME + ".gz"
MANIFEST_NAME = "latest.json"
NOTICE_NAME = "NOTICE"

NOTICE_TEXT = """\
This file (anime-db.sqlite.gz) is a derivative database built from:

  anime-offline-database
  https://github.com/manami-project/anime-offline-database
  Copyright (c) manami-project and contributors
  Database structure licensed under the Open Database License (ODbL) v1.0:
  https://opendatacommons.org/licenses/odbl/1-0/
  Individual contents licensed under the Database Contents License (DbCL) v1.0:
  https://opendatacommons.org/licenses/dbcl/1-0/

As a share-alike derivative database, this artifact (Sensarr's weekly
anime-db.sqlite.gz release, published from github.com/{repo}) is ALSO
distributed under the Open Database License v1.0. You are free to share,
create, and adapt it under that license, which requires attribution and
share-alike redistribution of anything you publish derived from it.

This artifact does NOT contain data from Fribb/anime-lists or
Anime-Lists/anime-lists. Neither project publishes a license file, so
Sensarr does not redistribute their id mappings. Sensarr clients fetch those
two small dumps directly from their own canonical GitHub URLs at refresh
time and merge them locally on your machine — they are never part of this
published artifact.

Built by: Sensarr (https://github.com/{repo})
"""


def fetch_manami(timeout: int = 180) -> dict:
    """Download the manami dump (same URL ladder anime_db.refresh() uses)."""
    manami = None
    for url in anime_db._MANAMI_URLS:
        try:
            print(f"Downloading anime-offline-database from {url} …")
            manami = json.loads(anime_db._download(url, timeout=timeout).decode("utf-8"))
            break
        except Exception as exc:
            print(f"  failed: {exc}", file=sys.stderr)
    if not isinstance(manami, dict) or not manami.get("data"):
        raise RuntimeError("anime-offline-database download failed from every URL")
    return manami


def _checkpoint_and_vacuum(db_path: Path) -> None:
    """WAL checkpoint before VACUUM: a WAL-mode SQLite file is not cleanly
    single-file until its WAL is folded back in, and a raw .sqlite copied
    mid-WAL is not a valid standalone database for a client that opens it
    directly (no matching -wal/-shm sidecars will ship in the artifact)."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
        conn.commit()
    finally:
        conn.close()
    for suffix in ("-wal", "-shm"):
        db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)


def package_artifact(manami: dict, output_dir: Path, *, tag: str = DEFAULT_TAG,
                     repo: str = DEFAULT_REPO) -> dict:
    """Build the manami-only DB, checkpoint+vacuum+gzip+hash it, and write
    latest.json + NOTICE into output_dir. Returns the manifest dict."""
    output_dir.mkdir(parents=True, exist_ok=True)
    db_path = output_dir / ARTIFACT_NAME
    db_path.unlink(missing_ok=True)

    anime_db.build_manami_artifact(manami, db_path)
    _checkpoint_and_vacuum(db_path)

    db_bytes = db_path.read_bytes()
    gz_bytes = gzip.compress(db_bytes, compresslevel=9, mtime=0)
    gz_path = output_dir / GZ_NAME
    gz_path.write_bytes(gz_bytes)

    sha256 = hashlib.sha256(gz_bytes).hexdigest()
    built = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest = {
        "schema_version": anime_db._SCHEMA_VERSION,
        "built": built,
        "sha256": sha256,
        "url": f"https://github.com/{repo}/releases/download/{tag}/{GZ_NAME}",
        "bytes": len(gz_bytes),
    }
    (output_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (output_dir / NOTICE_NAME).write_text(
        NOTICE_TEXT.format(repo=repo), encoding="utf-8")

    print(f"Built {ARTIFACT_NAME}: {len(manami.get('data', []))} entries, "
          f"schema {anime_db._SCHEMA_VERSION}")
    print(f"  {GZ_NAME}: {len(gz_bytes):,} bytes, sha256 {sha256}")
    print(f"  {MANIFEST_NAME} + {NOTICE_NAME} written to {output_dir}")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the weekly manami-only anime-db release artifact.")
    parser.add_argument("--output-dir", required=True, type=Path,
                        help="Directory to write anime-db.sqlite(.gz)/latest.json/NOTICE into.")
    parser.add_argument("--tag", default=DEFAULT_TAG,
                        help=f"Release tag the manifest URL points at (default: {DEFAULT_TAG}).")
    parser.add_argument("--repo", default=DEFAULT_REPO,
                        help=f"owner/repo the manifest URL points at (default: {DEFAULT_REPO}).")
    args = parser.parse_args()

    try:
        manami = fetch_manami()
        package_artifact(manami, args.output_dir, tag=args.tag, repo=args.repo)
    except Exception as exc:
        print(f"publish_anime_db failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
