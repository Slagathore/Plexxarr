// =============================================================================
// download.mjs — headless webtorrent downloader for PlexResetButton
// =============================================================================
// Usage: node download.mjs <magnet-uri> <destination-dir> [stallTimeoutSec]
//
// Protocol: newline-delimited JSON on stdout, consumed by download_manager.py:
//   {"event":"metadata","name":...,"files":[{"path":...,"size":...}]}
//   {"event":"progress","progress":0.42,"downloadSpeed":123456,"peers":7}
//   {"event":"done","name":...,"files":[{"path":...,"size":...}]}
//   {"event":"error","message":"..."}
//
// Seeding stops the moment the download completes (client.destroy on "done") —
// this runner never uploads beyond what the swarm gets during the download.
//
// Credit: the design of this pipeline (webtorrent engine, per-category
// sources, stop-seed-on-complete) is modeled on torlink by bairon
// (https://github.com/baairon/torlink, MIT). See README Acknowledgements.
// =============================================================================

import WebTorrent from "webtorrent";

const [magnet, destDir, stallTimeoutArg] = process.argv.slice(2);

// Deterministic no-network smoke mode (Task H item 9): prove the runner's
// dependency tree loads and a client constructs/destroys cleanly, with every
// peer-discovery mechanism disabled so nothing touches the network. CI runs
// this; process.exit here means the download flow below never starts.
if (magnet === "--smoke-test") {
  const smokeClient = new WebTorrent({
    dht: false, tracker: false, lsd: false, utPex: false, webSeeds: false,
  });
  console.log(JSON.stringify({ event: "smoke", ok: true, webtorrent: WebTorrent.VERSION || "unknown" }));
  const destroyed = new Promise((resolve) => smokeClient.destroy(resolve));
  const timedOut = new Promise((resolve) => setTimeout(() => resolve("timeout"), 10_000).unref());
  const result = await Promise.race([destroyed, timedOut]);
  if (result === "timeout") {
    console.log(JSON.stringify({ event: "error", message: "smoke destroy timed out" }));
    process.exit(1);
  }
  process.exit(0);
}

if (!magnet || !destDir) {
  console.log(JSON.stringify({ event: "error", message: "usage: download.mjs <magnet> <destDir> [stallSec]" }));
  process.exit(2);
}
const STALL_MS = (parseInt(stallTimeoutArg, 10) || 900) * 1000;

const emit = (obj) => console.log(JSON.stringify(obj));

const client = new WebTorrent();
let finished = false;

const die = (code) => {
  finished = true;
  client.destroy(() => process.exit(code));
  // Belt and braces: force-exit if destroy hangs.
  setTimeout(() => process.exit(code), 10_000).unref();
};

client.on("error", (err) => {
  emit({ event: "error", message: String(err.message || err) });
  die(1);
});

let lastDownloaded = 0;
let lastActivity = Date.now();

const torrent = client.add(magnet, { path: destDir });

torrent.on("error", (err) => {
  emit({ event: "error", message: String(err.message || err) });
  die(1);
});

torrent.on("metadata", () => {
  lastActivity = Date.now();
  emit({
    event: "metadata",
    name: torrent.name,
    files: torrent.files.map((f) => ({ path: f.path, size: f.length })),
  });
});

const progressTimer = setInterval(() => {
  if (finished) return;
  if (torrent.downloaded > lastDownloaded) {
    lastDownloaded = torrent.downloaded;
    lastActivity = Date.now();
  } else if (Date.now() - lastActivity > STALL_MS) {
    emit({ event: "error", message: `stalled: no data for ${STALL_MS / 1000}s` });
    clearInterval(progressTimer);
    die(1);
    return;
  }
  emit({
    event: "progress",
    progress: Number(torrent.progress.toFixed(4)),
    downloadSpeed: Math.round(torrent.downloadSpeed),
    peers: torrent.numPeers,
  });
}, 2000);

torrent.on("done", () => {
  clearInterval(progressTimer);
  emit({
    event: "done",
    name: torrent.name,
    files: torrent.files.map((f) => ({ path: f.path, size: f.length })),
  });
  die(0); // destroy immediately — stops seeding
});
