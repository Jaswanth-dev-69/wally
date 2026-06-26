#!/usr/bin/env python3
"""
Pinterest Wallpaper Manager for Zorin OS / GNOME
--------------------------------------------------
Each run: picks ONE search category at random (favoring categories/queries
not used last run), pulls a handful of 16:9 / >=1920px images from it via
gallery-dl, picks one that hasn't been shown recently, and sets it as the
GNOME desktop background.

Searching only one category per run (instead of all of them every time) is
what keeps this fast -- normally a single gallery-dl call. Spreading across
many categories plus remembering recently-shown images is what keeps the
results from cycling through the same handful of pictures.

Project layout (exactly as required):

    wally/
    |-- wallpaper.py
    `-- wallpaper/
        |-- wallpaper.jpg
        |-- .seen_hashes    <- last N image hashes shown (text, no images)
        `-- .last_query     <- last search category used (one line of text)
"""

import hashlib
import io
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
WALLPAPER_DIR = PROJECT_DIR / "wallpaper"
WALLPAPER_PATH = WALLPAPER_DIR / "wallpaper.jpg"
SEEN_HASHES_PATH = WALLPAPER_DIR / ".seen_hashes"
LAST_QUERY_PATH = WALLPAPER_DIR / ".last_query"

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
                             # can be multi-MB files; downloading more than a
                             # couple of them per run was the main reason
                             # this was slow.
HTTP_TIMEOUT = "10"          # fail fast instead of hanging on a slow request
RETRIES = "1"                # fewer retries on network hiccups = faster exit
MAX_ATTEMPTS = 3             # how many different categories to try in a
                             # single run before giving up (only kicks in if
                             # a category comes back empty or fully repeated)
SEEN_HASHES_MAX = 60         # remember this many recent wallpapers across
                             # ALL categories combined, oldest dropped first

# Run right at login (see autostart setup below), the network is sometimes
# not up yet for the first few seconds. Rather than a blind fixed sleep,
# the script polls a couple of fast, reliable hosts and proceeds the moment
# they're reachable -- so it's effectively instant once boot is actually
# done, and only waits as long as truly necessary otherwise.
NETWORK_CHECK_HOSTS = [("1.1.1.1", 443), ("8.8.8.8", 443)]
NETWORK_WAIT_TIMEOUT = 30    # give up waiting after this many seconds
NETWORK_CHECK_INTERVAL = 2   # seconds between connectivity checks

VALID_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff"}


def log(msg: str) -> None:
    print(f"[wallpaper] {msg}", flush=True)


def network_available() -> bool:
    for host, port in NETWORK_CHECK_HOSTS:
        try:
            with socket.create_connection((host, port), timeout=3):
                return True
        except OSError:
            continue
    return False


def wait_for_network() -> None:
    deadline = time.time() + NETWORK_WAIT_TIMEOUT
    announced = False
    while time.time() < deadline:
        if network_available():
            if announced:
                log("Network is up.")
            return
        if not announced:
            log("Network not ready yet (normal right after boot) -- "
                "waiting...")
            announced = True
        time.sleep(NETWORK_CHECK_INTERVAL)
    log("WARNING: network still unreachable after "
        f"{NETWORK_WAIT_TIMEOUT}s -- attempting anyway.")


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
    path.write_text("\n".join(lines) + "\n" if lines else "")


def choose_query(last_query: str | None, exclude: set[str]) -> str:
    avoid = set(exclude)
    if last_query:
        avoid.add(last_query)
    pool = [q for q in SEARCH_QUERIES if q not in avoid]
    if not pool:
        pool = SEARCH_QUERIES  # exhausted every option -- allow repeats
    return random.choice(pool)


# ---------------------------------------------------------------------------
# Step 1 -- download candidates for ONE query into a throwaway temp folder
# (auto-deletes itself, even if the script crashes).
# ---------------------------------------------------------------------------
def download_candidates(tmp_dir: Path, query: str) -> None:
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
    log(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log("gallery-dl exited non-zero (continuing -- some files may "
            "still have downloaded successfully).")
        if result.stderr:
            log(result.stderr.strip()[-800:])


# ---------------------------------------------------------------------------
# Step 2 -- local safety-net filter: width >= MIN_WIDTH AND ratio == 16:9.
# Confirms what gallery-dl's --filter should have already guaranteed.
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
    # Only ever called once per run, on the single winning image -- NOT on
    # every candidate. Decoding + re-encoding a genuine 8K image is real
    # CPU work, so this used to run up to DOWNLOAD_RANGE times for nothing.
    with Image.open(path) as img:
        rgb = img.convert("RGB")
        buf = io.BytesIO()
        rgb.save(buf, "JPEG", quality=92)
        return buf.getvalue()


def hash_bytes(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def hash_file(path: Path) -> str:
    # Identity check for "have I shown this before" -- hashes the raw
    # downloaded bytes directly, no image decode required. Cheap even for
    # huge files, and that's all we need it for.
    return hash_bytes(path.read_bytes())


# ---------------------------------------------------------------------------
# Step 3 -- write the winner OVER the existing wallpaper.jpg IN PLACE.
#
# This is the fix for the original "gsettings shows the right path but the
# desktop never refreshes" bug. Deleting wallpaper.jpg and creating a new
# file with the same name breaks the inotify watch GNOME uses on that
# inode. Writing the new bytes into the SAME open file (O_TRUNC, no
# unlink) keeps the inode -- and the watch -- intact.
# ---------------------------------------------------------------------------
def write_wallpaper(jpeg_bytes: bytes) -> None:
    WALLPAPER_DIR.mkdir(parents=True, exist_ok=True)
    with open(WALLPAPER_PATH, "wb") as f:
        f.write(jpeg_bytes)
    log(f"Saved winning wallpaper -> {WALLPAPER_PATH}")


# ---------------------------------------------------------------------------
# Step 4 -- force GNOME to actually repaint the background.
# ---------------------------------------------------------------------------
def apply_wallpaper() -> None:
    uri = f"file://{WALLPAPER_PATH}"

    def gset(key: str, value: str) -> None:
        subprocess.run(
            ["gsettings", "set", "org.gnome.desktop.background", key, value],
            check=False,
        )

    for key in ("picture-uri", "picture-uri-dark"):
        gset(key, "")
    gset("picture-options", "zoom")
    for key in ("picture-uri", "picture-uri-dark"):
        gset(key, uri)

    log("Applied wallpaper via gsettings (picture-uri + picture-uri-dark).")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    if shutil.which("gallery-dl") is None:
        log("ERROR: gallery-dl is not installed or not on PATH.")
        log("Install it with: pip install --user gallery-dl")
        return 1
    if shutil.which("gsettings") is None:
        log("ERROR: gsettings not found -- is this really a GNOME session?")
        return 1

    wait_for_network()

    WALLPAPER_DIR.mkdir(parents=True, exist_ok=True)

    seen_hashes = read_lines(SEEN_HASHES_PATH)
    seen_set = set(seen_hashes)
    last_query = (read_lines(LAST_QUERY_PATH) or [None])[0]

    tried_queries: set[str] = set()
    winner_bytes = None
    winner_hash = None
    used_query = None
    fallback = None  # (bytes, hash, query) -- best we found even if "seen"

    for attempt in range(1, MAX_ATTEMPTS + 1):
        query = choose_query(last_query, tried_queries)
        tried_queries.add(query)
        log(f"Attempt {attempt}/{MAX_ATTEMPTS}: searching Pinterest for "
            f"'{query}'...")

        with tempfile.TemporaryDirectory(prefix="pinterest_wp_") as tmp:
            tmp_dir = Path(tmp)
            download_candidates(tmp_dir, query)
            candidates = find_valid_wallpapers(tmp_dir)
            log(f"Found {len(candidates)} valid 16:9 wallpaper(s) "
                f"(width >= {MIN_WIDTH}px) for '{query}'.")

            if not candidates:
                continue

            # Cheap identity check on the raw downloaded bytes -- no image
            # decoding needed here, so this stays fast even for huge files.
            scored = [(path, hash_file(path)) for path in candidates]
            fresh = [(p, h) for p, h in scored if h not in seen_set]

            if fresh:
                chosen_path, _ = random.choice(fresh)
                # The ONLY decode + JPEG re-encode this run does, and only
                # for the single image that actually wins.
                winner_bytes = encode_jpeg(chosen_path)
                winner_hash = hash_bytes(winner_bytes)
                used_query = query
                break

            if fallback is None:
                chosen_path, _ = random.choice(scored)
                fb_bytes = encode_jpeg(chosen_path)
                fallback = (fb_bytes, hash_bytes(fb_bytes), query)

        # tmp_dir and every losing candidate are now gone -- nothing leftover.

    if not winner_bytes and fallback:
        log("WARNING: every candidate across the categories tried matches "
            "something shown recently -- reusing one anyway so the desktop "
            "still changes from whatever it currently is.")
        winner_bytes, winner_hash, used_query = fallback

    if not winner_bytes:
        log(f"ERROR: No valid 16:9 wallpapers found after {MAX_ATTEMPTS} "
            "attempts. Nothing was changed.")
        return 1

    write_wallpaper(winner_bytes)

    seen_hashes.append(winner_hash)
    write_lines(SEEN_HASHES_PATH, seen_hashes[-SEEN_HASHES_MAX:])
    write_lines(LAST_QUERY_PATH, [used_query])

    apply_wallpaper()
    log(f"Done. (category: '{used_query}')")
    return 0


if __name__ == "__main__":
    sys.exit(main())
