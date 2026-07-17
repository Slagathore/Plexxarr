# =============================================================================
# platform_adapter.py — Task H item 3
# =============================================================================
# The one place OS capabilities are decided, instead of `if sys.platform`
# scattered through desktop code. Everything here is import-light: no tkinter,
# no PIL, no pystray at module import — the tray stack loads lazily so a
# missing Linux backend degrades to "tray unavailable" instead of an
# ImportError killing the app.
#
# Windows behavior is unchanged by design: the same MessageBoxW duplicate
# dialog, os.startfile, winget guidance, taskkill-based process control (in
# plex_control), and .bat self-updater as before, just reached through here.
# =============================================================================

import logging
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


def is_windows() -> bool:
    return sys.platform == "win32"


# ---------------------------------------------------------------------------
# Duplicate-instance dialog (main.py guard)
# ---------------------------------------------------------------------------

def show_duplicate_instance_message(message: str, title: str = "Sensarr") -> None:
    """Tell the user a second instance was refused. Windows keeps the exact
    ctypes MessageBoxW call it always used; elsewhere try a Tk messagebox and
    fall back to stderr — the message must never crash the guard itself."""
    if is_windows():
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, title, 0x30)  # type: ignore[attr-defined]
        return
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning(title, message)
        root.destroy()
        return
    except Exception:
        logger.debug("Tk unavailable for the duplicate-instance dialog.",
                     exc_info=True)
    print(f"{title}: {message}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Opening folders/files in the OS file manager
# ---------------------------------------------------------------------------

def open_path(path: str | Path) -> bool:
    """Open a folder (or file) with the platform's opener. Returns False when
    no opener is available instead of raising."""
    path = str(path)
    if is_windows():
        os_startfile = getattr(__import__("os"), "startfile", None)
        if os_startfile is None:  # pragma: no cover — win32 always has it
            return False
        os_startfile(path)  # noqa: S606 — local folder open on Windows
        return True
    opener = shutil.which("xdg-open")
    if opener is None:
        logger.warning("xdg-open not found — cannot open %s "
                       "(install xdg-utils).", path)
        return False
    try:
        subprocess.Popen([opener, path], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        return True
    except OSError:
        logger.warning("xdg-open failed for %s", path, exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Tray availability (PIL + pystray, both lazy/optional now)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TraySupport:
    available: bool
    reason: str          # human-readable when unavailable
    pystray: object | None   # the imported pystray module (or None)


_tray_cache: TraySupport | None = None


def tray_probe() -> tuple[bool, str]:
    """SIDE-EFFECT-FREE tray availability estimate, for diagnostics only.

    Uses importlib.util.find_spec plus environment checks — it never imports
    pystray, whose Linux backend selection spawns a subprocess (`uname -p`)
    at import time. The diagnostics/guidance path must execute nothing; only
    the real tray-creation path (tray_support) may import."""
    import importlib.util
    import os
    missing = [label for label, pkg in (("Pillow", "PIL"),
                                        ("pystray", "pystray"))
               if importlib.util.find_spec(pkg) is None]
    if missing:
        hint = ("install the missing package(s)" if is_windows() else
                "they install with 'pip install -r requirements.txt'")
        return False, f"missing package(s): {', '.join(missing)} — {hint}"
    if not is_windows():
        if all(importlib.util.find_spec(pkg) is None for pkg in ("Xlib", "gi")):
            return False, ("no tray backend installed (X11: pip package "
                           "python-xlib; GNOME/KDE: system PyGObject + an "
                           "AppIndicator library)")
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            return False, ("no display session (DISPLAY/WAYLAND_DISPLAY "
                           "unset) — the tray needs a desktop session")
    return True, ""


def tray_support(refresh: bool = False) -> TraySupport:
    """Import the tray stack on demand. On Linux, pystray raises at import
    when no backend (AppIndicator/GTK/X11) is usable — that becomes a
    visible 'tray unavailable' state, never a startup crash."""
    global _tray_cache
    if _tray_cache is not None and not refresh:
        return _tray_cache
    try:
        import PIL.Image  # noqa: F401 — pystray renders icons through PIL
        import PIL.ImageDraw  # noqa: F401
        import pystray
    except Exception as exc:
        hint = ("install the 'pystray' and 'Pillow' packages"
                if is_windows() else
                "install a tray backend (X11: pip package python-xlib; "
                "GNOME/KDE: system PyGObject + an AppIndicator library)")
        _tray_cache = TraySupport(False, f"{exc} — {hint}", None)
        logger.warning("System tray unavailable: %s", _tray_cache.reason)
        return _tray_cache
    _tray_cache = TraySupport(True, "", pystray)
    return _tray_cache


# ---------------------------------------------------------------------------
# Dependency-install guidance (setup wizard / diagnostics)
# ---------------------------------------------------------------------------

# The Ubuntu line below is exercised verbatim by the linux-smoke CI job —
# keep .github/workflows/linux-smoke.yml in sync when editing it.
UBUNTU_APT_LINE = "sudo apt install python3-tk ffmpeg xdg-utils"


def supports_winget() -> bool:
    return is_windows()


def dependency_install_guidance() -> str:
    """Diagnostics text naming exactly what's missing and how to get it.
    Never runs a package manager and never asks for root — and never runs
    ANY subprocess: availability comes from find_spec/which/env only
    (tests/test_task_h.py asserts zero subprocess calls in here)."""
    if is_windows():
        return ("Use the 'Install checked (via winget)' button above, or "
                "install Node.js LTS and FFmpeg manually.")

    import importlib.util
    lines: list[str] = []
    if importlib.util.find_spec("tkinter") is None:
        lines.append("- tkinter missing: the desktop UI cannot start "
                     "(Ubuntu package: python3-tk).")
    if shutil.which("ffprobe") is None:
        lines.append("- ffprobe missing: runtime probing falls back to "
                     "assumptions (Ubuntu package: ffmpeg).")
    if shutil.which("xdg-open") is None:
        lines.append("- xdg-open missing: 'open folder' buttons are disabled "
                     "(Ubuntu package: xdg-utils).")
    if shutil.which("node") is None:
        lines.append("- node missing: the torrent Downloads tab cannot start "
                     "downloads (install Node.js 20+ from nodejs.org or your "
                     "distro).")
    tray_ok, tray_reason = tray_probe()
    if not tray_ok:
        lines.append(f"- system tray unavailable: {tray_reason}")
    if not lines:
        lines.append("- all required commands were found.")
    return (
        "Linux setup is manual on purpose — the app never runs apt/dnf/pacman "
        "and never asks for root.\n\n"
        + "\n".join(lines)
        + "\n\nOn Ubuntu/Debian the OS packages come from:\n"
        f"  {UBUNTU_APT_LINE}\n"
        "The selected tray backend (X11 via python-xlib) installs with "
        "'pip install -r requirements.txt'. On other distros install the "
        "same tools with your package manager (Tk for Python, ffmpeg, "
        "xdg-utils). Node.js 20+ and Ollama install per their own docs."
    )


# ---------------------------------------------------------------------------
# Process control (Linux path — Windows keeps taskkill in plex_control)
# ---------------------------------------------------------------------------

def terminate_process_tree(pid: int, *, timeout: float = 10.0) -> tuple[bool, str]:
    """POSIX process-tree stop: children first, terminate -> timed wait ->
    kill. Never shells out to taskkill. Returns (all_gone, detail)."""
    import psutil
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return True, "already exited"
    try:
        procs = parent.children(recursive=True) + [parent]
    except psutil.NoSuchProcess:
        return True, "already exited"

    for proc in procs:  # children first (list order), parent last
        try:
            proc.terminate()
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            return False, f"access denied terminating pid {proc.pid}"

    _gone, alive = psutil.wait_procs(procs, timeout=timeout)
    for proc in alive:
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            return False, f"access denied killing pid {proc.pid}"
    if alive:
        _gone2, alive = psutil.wait_procs(alive, timeout=timeout)
    if alive:
        return False, f"{len(alive)} process(es) survived kill"
    return True, "terminated"


# ---------------------------------------------------------------------------
# Updater capability
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UpdaterCapability:
    kind: str    # "windows-self-update" | "linux-source" | "linux-packaged"
    can_self_update: bool
    hint: str    # honest instructions when self-update is off


def updater_capability() -> UpdaterCapability:
    frozen = bool(getattr(sys, "frozen", False))
    if is_windows():
        return UpdaterCapability(
            "windows-self-update", frozen,
            "" if frozen else "Source checkout: update with 'git pull'.")
    if frozen:
        return UpdaterCapability(
            "linux-packaged", False,
            "Self-update is not available in the Linux build yet. Download "
            "the new Linux artifact from the release page and replace this "
            "install; your settings and databases live under ~/.config and "
            "~/.local/share and are untouched.")
    return UpdaterCapability(
        "linux-source", False,
        "Source checkout: update with 'git pull' (then reinstall "
        "requirements if the release notes say so).")


# ---------------------------------------------------------------------------
# CI self-test: spawn a harmless fixture tree and stop it through the adapter
# ---------------------------------------------------------------------------

def _selftest() -> int:  # pragma: no cover — exercised by linux-smoke CI
    import time
    child = subprocess.Popen([
        sys.executable, "-c",
        "import subprocess, sys, time;"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
        "time.sleep(60)",
    ])
    time.sleep(1.0)  # let the grandchild spawn
    ok, detail = terminate_process_tree(child.pid, timeout=10.0)
    print(f"terminate_process_tree: ok={ok} detail={detail}")
    if not ok:
        return 1
    caps = updater_capability()
    print(f"updater capability: {caps.kind} self_update={caps.can_self_update}")
    tray = tray_support()
    print(f"tray available: {tray.available}"
          + ("" if tray.available else f" ({tray.reason})"))
    print("platform_adapter selftest PASS")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
