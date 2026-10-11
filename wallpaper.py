#!/usr/bin/env python3
"""
Pinterest Wallpaper Manager for Zorin OS / GNOME
--------------------------------------------------
Each run: picks ONE search category at random (favoring categories/queries
not used last run), pulls a handful of 16:9 / >=1920px images from it via
gallery-dl, picks one that hasn't been shown recently, and sets it as the
GNOME desktop background.

Safety rules (this script is started in the background right after login, so
it must never leave the desktop without a valid background):

  * Only one instance runs at a time (flock on a lock file).
  * Waits for the desktop and for the network, each with a hard maximum, and
    gives up quietly (exit 0) so the systemd timer can retry later.
  * Downloads into a persistent scratch dir (~/.cache/wally, not /tmp),
    wrapped in `timeout` so gallery-dl can never hang.
  * The new image is validated with Pillow, written to a temp file inside
    the wallpaper folder and moved over wallpaper.jpg with os.replace()
    (atomic). On any failure the existing wallpaper.jpg is left untouched.
  * gsettings is only ever called with the path of a file that exists, is
    non-empty and decodes as an image. It is never given "" or a directory.

Project layout:

    wally/
    |-- wallpaper.py
    `-- wallpaper/
        |-- wallpaper.jpg
        |-- .seen_hashes    <- last N image hashes shown (text, no images)
        `-- .last_query     <- last search category used (one line of text)
"""

import argparse
import fcntl
import hashlib
import io
import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

from PIL import Image

# ---------------------------------------------------------------------------
# Configuration -- tweak freely
# ---------------------------------------------------------------------------
PROJECT_DIR = Path(__file__).resolve().parent
# WALLY_WALLPAPER_DIR / WALLY_CACHE_DIR exist so the tests can run against
# throwaway directories; normal use never sets them.
WALLPAPER_DIR = Path(os.environ.get("WALLY_WALLPAPER_DIR")
                     or PROJECT_DIR / "wallpaper")
WALLPAPER_PATH = WALLPAPER_DIR / "wallpaper.jpg"
SEEN_HASHES_PATH = WALLPAPER_DIR / ".seen_hashes"
LAST_QUERY_PATH = WALLPAPER_DIR / ".last_query"

# Persistent scratch space (NOT /tmp): downloads and the lock file live here.
CACHE_DIR = Path(os.environ.get("WALLY_CACHE_DIR")
                 or Path(os.environ.get("XDG_CACHE_HOME")
                         or Path.home() / ".cache") / "wally")
LOCK_PATH = CACHE_DIR / "wally.lock"

TIMER_UNIT = "wally.timer"   # stopped after a successful --once-per-login run

SEARCH_QUERIES = [
    "viltrumite pc wallpaper",
    "8k wallpapers for pc",
    "anime wallpaper for pc",
    "couples wallpaper for pc",
    "ai wallpaper for pc",
    "gym wallpaper for pc",
    "solitude wallpaper for pc",
    "pc wallpaper aesthetic",
    "dark aesthetic wallpaper pc",
    "marvel aesthetic wallpaper",
    "marvel 4k pc wallpaper",
    "overstimulated wallpaper pc",
    "overstimulation core wallpaper",
    "dopamine aesthetic wallpaper pc",
    "maximalist aesthetic wallpaper pc",
    "y2k aesthetic wallpaper pc",
    "vaporwave aesthetic wallpaper pc",
]

MIN_WIDTH = 1920
TARGET_RATIO = 16 / 9
RATIO_TOLERANCE = 0.02       # real Pinterest images are rarely pixel-perfect
                             # 16:9 -- this lets through e.g. 1920x1083 while
                             # still rejecting 4:3, square, or portrait shots.
                             # Enforced BOTH server-side (gallery-dl skips
                             # non-matches before downloading) and again
                             # locally with Pillow as a safety net.

DOWNLOAD_RANGE = "1-3"      # matching pins to fetch from the ONE category
                             # picked this run. Only ONE of these ever gets
                             # used -- kept small on purpose because several
                             # categories ("8k wallpapers", "4k pc wallpaper")
                             # can be multi-MB files.
HTTP_TIMEOUT = "10"          # fail fast instead of hanging on a slow request
RETRIES = "1"                # fewer retries on network hiccups = faster exit
GALLERY_DL_TIMEOUT = 60      # hard wall-clock cap (seconds) per gallery-dl
                             # call, enforced with `timeout`
MAX_ATTEMPTS = 3             # how many different categories to try in a
                             # single run before giving up
SEEN_HASHES_MAX = 60         # remember this many recent wallpapers across
                             # ALL categories combined, oldest dropped first

# Started shortly after login, the desktop and network may not be up yet.
# Both waits have a hard maximum; if exceeded the script exits 0 and the
# systemd timer simply tries again a few minutes later.
DESKTOP_WAIT_TIMEOUT = 120   # seconds to wait for gnome-shell
NETWORK_WAIT_TIMEOUT = 30    # seconds to wait for connectivity
NETWORK_CHECK_INTERVAL = 2   # seconds between connectivity checks
# host:port probes; a successful TCP connect to ANY of them means "online".
# The first one also proves DNS works, which gallery-dl needs.
NETWORK_CHECK_HOSTS = [("www.pinterest.com", 443), ("1.1.1.1", 443)]

VALID_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff"}


def log(msg: str) -> None:
    print(f"[wallpaper] {msg}", flush=True)


def _network_hosts() -> list[tuple[str, int]]:
    # WALLY_NETWORK_HOSTS="host:port,host:port" overrides the probes (tests).
    raw = os.environ.get("WALLY_NETWORK_HOSTS")
    if not raw:
        return NETWORK_CHECK_HOSTS
    hosts = []
    for item in raw.split(","):
        host, _, port = item.strip().rpartition(":")
        if host and port.isdigit():
            hosts.append((host, int(port)))
    return hosts


# ---------------------------------------------------------------------------
# Waiting for the session
# ---------------------------------------------------------------------------
def network_available() -> bool:
    for host, port in _network_hosts():
        try:
            with socket.create_connection((host, port), timeout=3):
                return True
        except OSError:
            continue
    return False


def wait_for_network(timeout: float) -> bool:
    """True once online; False if still offline after `timeout` seconds."""
    deadline = time.monotonic() + timeout
    announced = False
    while True:
        if network_available():
            if announced:
                log("Network is up.")
            return True
        if time.monotonic() >= deadline:
            return False
        if not announced:
            log("Network not ready yet (normal right after login) -- "
                "waiting...")
            announced = True
        time.sleep(min(NETWORK_CHECK_INTERVAL,
                       max(0.0, deadline - time.monotonic())))


def desktop_running() -> bool:
    return subprocess.run(
        ["pgrep", "-u", str(os.getuid()), "-x", "gnome-shell"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0


def wait_for_desktop(timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while not desktop_running():
        if time.monotonic() >= deadline:
            return False
        time.sleep(2)
    return True


def ensure_session_bus_env() -> None:
    # Started from a systemd user unit or autostart, gsettings can end up
    # without a bus address and then silently writes nothing.
    if os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return
    bus = Path(f"/run/user/{os.getuid()}/bus")
    if bus.exists():
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"


# ---------------------------------------------------------------------------
# Single-instance lock
# ---------------------------------------------------------------------------
def acquire_lock():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_PATH, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


# ---------------------------------------------------------------------------
# Tiny text-file helpers -- store hashes/category names, never images.
# ---------------------------------------------------------------------------
def read_lines(path: Path) -> list[str]:
    try:
        return [line.strip() for line in path.read_text().splitlines()
                 if line.strip()]
    except FileNotFoundError:
        return []


def write_lines(path: Path, lines: list[str]) -> None:
    # Atomic too, so a crash can't leave a half-written state file.
    tmp = path.with_name(path.name + ".new")
    tmp.write_text("\n".join(lines) + "\n" if lines else "")
    os.replace(tmp, path)


def choose_query(last_query: str | None, exclude: set[str]) -> str:
    avoid = set(exclude)
    if last_query:
        avoid.add(last_query)
    pool = [q for q in SEARCH_QUERIES if q not in avoid]
    if not pool:
        pool = SEARCH_QUERIES  # exhausted every option -- allow repeats
    return random.choice(pool)


# ---------------------------------------------------------------------------
# Step 1 -- download candidates for ONE query into a scratch folder.
# ---------------------------------------------------------------------------
def download_candidates(tmp_dir: Path, query: str) -> bool:
    url = f"https://www.pinterest.com/search/pins/?q={quote(query)}"

    # Server-side filter: gallery-dl checks each pin's own width/height
    # metadata BEFORE downloading. Anything that fails costs a metadata
    # lookup only -- no image transfer. This is the main speed lever.
    filter_expr = (
        f"width and height and width >= {MIN_WIDTH} "
        f"and abs(width / height - {TARGET_RATIO}) <= {RATIO_TOLERANCE}"
    )

    cmd = [
        "gallery-dl",
        "--range", DOWNLOAD_RANGE,
        "--filter", filter_expr,
        "--http-timeout", HTTP_TIMEOUT,
        "-R", RETRIES,
        "-o", "pinterest.videos=false",   # skip video pins entirely
        "-o", "pinterest.stories=false",  # skip story pins entirely
        "-d", str(tmp_dir),
        url,
    ]
    # `timeout` guarantees gallery-dl can't hang: SIGTERM after the limit,
    # SIGKILL 5s later if it ignores that.
    if shutil.which("timeout"):
        cmd = ["timeout", "--kill-after=5", str(GALLERY_DL_TIMEOUT)] + cmd
        py_timeout = None
    else:
        py_timeout = GALLERY_DL_TIMEOUT + 5

    log(f"Running: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=py_timeout)
    except subprocess.TimeoutExpired:
        log(f"gallery-dl timed out after {GALLERY_DL_TIMEOUT}s.")
        return False
    if result.returncode == 124:
        log(f"gallery-dl timed out after {GALLERY_DL_TIMEOUT}s.")
    elif result.returncode != 0:
        log("gallery-dl exited non-zero (continuing -- some files may "
            "still have downloaded successfully).")
        if result.stderr:
            log(result.stderr.strip()[-800:])
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Step 2 -- local safety-net filter: width >= MIN_WIDTH AND ratio == 16:9.
# ---------------------------------------------------------------------------
def find_valid_wallpapers(tmp_dir: Path) -> list[Path]:
    valid = []
    for path in tmp_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in VALID_EXTENSIONS:
            continue
        try:
            with Image.open(path) as img:
                w, h = img.size
        except Exception:
            continue  # corrupt/partial download, not a real image, etc.

        if w < MIN_WIDTH:
            continue
        if abs((w / h) - TARGET_RATIO) > RATIO_TOLERANCE:
            continue

        valid.append(path)
    return valid


def encode_jpeg(path: Path) -> bytes:
    # Only ever called on the single winning image -- NOT on every candidate.
    with Image.open(path) as img:
        rgb = img.convert("RGB")
        buf = io.BytesIO()
        rgb.save(buf, "JPEG", quality=92)
        return buf.getvalue()


def hash_bytes(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def hash_file(path: Path) -> str:
    return hash_bytes(path.read_bytes())


def is_valid_image_file(path: Path) -> bool:
    """Regular, non-empty file that Pillow can fully decode."""
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return False
        with Image.open(path) as img:
            img.load()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Step 3 -- replace wallpaper.jpg ATOMICALLY.
#
# The new image is fully written (and fsync'd) to a temp file in the same
# folder, validated, then moved over wallpaper.jpg with rename(2). At every
# instant wallpaper.jpg is either the complete old image or the complete new
# one -- never missing, empty or half-written, which is what GNOME Shell
# could previously catch mid-write at login.
# ---------------------------------------------------------------------------
def write_wallpaper(jpeg_bytes: bytes) -> bool:
    WALLPAPER_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".wallpaper-", suffix=".jpg.new",
                                    dir=WALLPAPER_DIR)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(jpeg_bytes)
            f.flush()
            os.fsync(f.fileno())
        if not is_valid_image_file(tmp):
            log("New wallpaper failed validation -- keeping the existing one.")
            return False
        os.chmod(tmp, 0o644)
        os.replace(tmp, WALLPAPER_PATH)
    finally:
        tmp.unlink(missing_ok=True)
    log(f"Saved winning wallpaper -> {WALLPAPER_PATH}")
    return True


# ---------------------------------------------------------------------------
# Step 4 -- tell GNOME to use it (never with an empty/missing path).
# ---------------------------------------------------------------------------
def _gsettings(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gsettings", *args], capture_output=True,
                          text=True, timeout=20)


def apply_wallpaper() -> bool:
    if not is_valid_image_file(WALLPAPER_PATH):
        log(f"Refusing to call gsettings: {WALLPAPER_PATH} is missing, "
            "empty or not a valid image.")
        return False

    uri = WALLPAPER_PATH.resolve().as_uri()
    # GNOME only reloads when the key's *string* changes. The old code forced
    # that by briefly setting "" (an empty background -- and the source of
    # "Failed to load background 'file:///home/<user>'"). Instead, if the
    # value is already the same, flip through an equivalent spelling of the
    # same existing file ("/./") first. Every value ever set points at a real
    # file.
    alt_uri = uri.replace("/wallpaper.jpg", "/./wallpaper.jpg")
    try:
        for key in ("picture-uri", "picture-uri-dark"):
            current = _gsettings("get", "org.gnome.desktop.background", key)
            if current.returncode == 0 and uri in current.stdout:
                _gsettings("set", "org.gnome.desktop.background", key,
                           alt_uri)
        _gsettings("set", "org.gnome.desktop.background",
                   "picture-options", "zoom")
        ok = True
        for key in ("picture-uri", "picture-uri-dark"):
            r = _gsettings("set", "org.gnome.desktop.background", key, uri)
            ok = ok and r.returncode == 0
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"gsettings failed: {exc}")
        return False
    if ok:
        log("Applied wallpaper via gsettings (picture-uri + "
            "picture-uri-dark).")
    else:
        log("WARNING: gsettings reported an error; wallpaper.jpg was "
            "updated but the setting may not have changed.")
    return ok


def stop_retry_timer() -> None:
    subprocess.run(["systemctl", "--user", "stop", TIMER_UNIT],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   check=False)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def pick_wallpaper(scratch: Path):
    """Download + validate. Returns (jpeg_bytes, hash, query) or None."""
    seen_set = set(read_lines(SEEN_HASHES_PATH))
    last_query = (read_lines(LAST_QUERY_PATH) or [None])[0]
    tried_queries: set[str] = set()
    fallback = None  # best we found even if "seen"

    for attempt in range(1, MAX_ATTEMPTS + 1):
        query = choose_query(last_query, tried_queries)
        tried_queries.add(query)
        log(f"Attempt {attempt}/{MAX_ATTEMPTS}: searching Pinterest for "
            f"'{query}'...")

        attempt_dir = scratch / f"attempt{attempt}"
        attempt_dir.mkdir()
        download_candidates(attempt_dir, query)
        candidates = find_valid_wallpapers(attempt_dir)
        log(f"Found {len(candidates)} valid 16:9 wallpaper(s) "
            f"(width >= {MIN_WIDTH}px) for '{query}'.")
        if not candidates:
            continue

        scored = [(path, hash_file(path)) for path in candidates]
        fresh = [(p, h) for p, h in scored if h not in seen_set]
        if fresh:
            chosen_path, _ = random.choice(fresh)
            data = encode_jpeg(chosen_path)
            return data, hash_bytes(data), query

        if fallback is None:
            chosen_path, _ = random.choice(scored)
            data = encode_jpeg(chosen_path)
            fallback = (data, hash_bytes(data), query)

    if fallback:
        log("WARNING: every candidate across the categories tried matches "
            "something shown recently -- reusing one anyway.")
    return fallback


def run(dry_run: bool, once_per_login: bool,
        network_timeout: float, desktop_timeout: float) -> int:
    if shutil.which("gallery-dl") is None:
        log("ERROR: gallery-dl is not installed or not on PATH.")
        log("Install it with: pip install --user gallery-dl")
        return 1
    if shutil.which("gsettings") is None and not dry_run:
        log("ERROR: gsettings not found -- is this really a GNOME session?")
        return 1

    lock = acquire_lock()
    if lock is None:
        log("Another instance is already running -- exiting.")
        return 0

    with lock:
        if not dry_run and desktop_timeout > 0 \
                and not wait_for_desktop(desktop_timeout):
            log("Desktop (gnome-shell) not up yet -- giving up for now; "
                "will retry later.")
            return 0
        ensure_session_bus_env()

        if not wait_for_network(network_timeout):
            log(f"No network after {network_timeout:.0f}s -- giving up for "
                "now; will retry later. Wallpaper left untouched.")
            return 0

        # Leftovers from a crashed run are safe to delete: we hold the lock.
        scratch_root = CACHE_DIR / "tmp"
        shutil.rmtree(scratch_root, ignore_errors=True)
        scratch_root.mkdir(parents=True)
        try:
            picked = pick_wallpaper(scratch_root)
            if picked is None:
                log(f"No valid 16:9 wallpapers found after {MAX_ATTEMPTS} "
                    "attempts. Wallpaper left untouched; will retry later.")
                return 0

            jpeg_bytes, jpeg_hash, used_query = picked
            if dry_run:
                log(f"DRY RUN: would set a {len(jpeg_bytes)}-byte wallpaper "
                    f"from '{used_query}'. Nothing was changed.")
                return 0

            if not write_wallpaper(jpeg_bytes):
                return 0

            seen = read_lines(SEEN_HASHES_PATH)
            seen.append(jpeg_hash)
            write_lines(SEEN_HASHES_PATH, seen[-SEEN_HASHES_MAX:])
            write_lines(LAST_QUERY_PATH, [used_query])

            apply_wallpaper()
            log(f"Done. (category: '{used_query}')")
            if once_per_login:
                stop_retry_timer()
            return 0
        finally:
            shutil.rmtree(scratch_root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="wait for network, download and validate, but "
                             "do not touch wallpaper.jpg, state files or "
                             "gsettings")
    parser.add_argument("--once-per-login", action="store_true",
                        help="after a successful update, stop wally.timer so "
                             "no further retries happen until next login "
                             "(used by the systemd service)")
    parser.add_argument("--network-timeout", type=float,
                        default=NETWORK_WAIT_TIMEOUT, metavar="SEC",
                        help="max seconds to wait for network "
                             f"(default {NETWORK_WAIT_TIMEOUT})")
    parser.add_argument("--desktop-timeout", type=float,
                        default=0, metavar="SEC",
                        help="max seconds to wait for gnome-shell to be "
                             "running (default 0 = don't wait; the systemd "
                             f"service passes {DESKTOP_WAIT_TIMEOUT})")
    args = parser.parse_args(argv)
    return run(args.dry_run, args.once_per_login,
               args.network_timeout, args.desktop_timeout)


if __name__ == "__main__":
    sys.exit(main())
