"""End-to-end tests for wallpaper.py using stub `gallery-dl` / `gsettings`.

Run:  python3 -m unittest discover -s tests -v

No network or GNOME session is needed. Each test gets throwaway wallpaper and
cache dirs, and a PATH where `gallery-dl` and `gsettings` are small fakes:
  * fake gallery-dl writes a real 1920x1080 image into the -d directory
    (or fails / hangs, depending on FAKE_GALLERY_MODE)
  * fake gsettings appends its arguments to $FAKE_GSETTINGS_LOG
"""
import os
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "wallpaper.py"

FAKE_GALLERY = textwrap.dedent("""\
    #!/usr/bin/env python3
    import os, sys
    from PIL import Image
    mode = os.environ.get("FAKE_GALLERY_MODE", "ok")
    if mode == "fail":
        sys.exit(1)
    if mode == "hang":
        import time; time.sleep(600)
    out = sys.argv[sys.argv.index("-d") + 1]
    os.makedirs(out, exist_ok=True)
    if mode == "garbage":
        open(os.path.join(out, "x.jpg"), "wb").write(b"not an image")
    else:
        Image.effect_noise((1920, 1080), 80).convert("RGB").save(
            os.path.join(out, "x.jpg"))
""")

FAKE_GSETTINGS = textwrap.dedent("""\
    #!/bin/sh
    echo "$@" >> "$FAKE_GSETTINGS_LOG"
    [ "$1" = get ] && echo "''"
    exit 0
""")


def make_image(path: Path, color: int) -> bytes:
    Image.new("RGB", (1920, 1080), (color, color, color)).save(path, "JPEG")
    return path.read_bytes()


class WallpaperTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        td = Path(self._td.name)
        self.wall_dir = td / "wallpaper"
        self.cache_dir = td / "cache"
        self.wall_dir.mkdir()
        bindir = td / "bin"
        bindir.mkdir()
        for name, body in (("gallery-dl", FAKE_GALLERY),
                           ("gsettings", FAKE_GSETTINGS)):
            p = bindir / name
            p.write_text(body)
            p.chmod(p.stat().st_mode | stat.S_IXUSR)
        self.gs_log = td / "gsettings.log"
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(5)
        self.online_port = self.listener.getsockname()[1]
        # A port that is guaranteed closed: bind, read the port, close.
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.offline_port = s.getsockname()[1]
        s.close()
        self.env = {
            **os.environ,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "WALLY_WALLPAPER_DIR": str(self.wall_dir),
            "WALLY_CACHE_DIR": str(self.cache_dir),
            "FAKE_GSETTINGS_LOG": str(self.gs_log),
            "WALLY_NETWORK_HOSTS": f"127.0.0.1:{self.online_port}",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent",
        }
        self.wallpaper = self.wall_dir / "wallpaper.jpg"
        self.original = make_image(self.wallpaper, 40)

    def tearDown(self):
        self.listener.close()
        self._td.cleanup()

    def run_script(self, *args, **env):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            env={**self.env, **env}, capture_output=True, text=True,
            timeout=120)

    def gsettings_calls(self):
        return self.gs_log.read_text().splitlines() if self.gs_log.exists() \
            else []

    def assert_untouched(self):
        self.assertEqual(self.wallpaper.read_bytes(), self.original)
        self.assertEqual(self.gsettings_calls(), [])
        self.assertEqual(list(self.wall_dir.glob(".wallpaper-*")), [])

    # -- the scenario from the bug report ---------------------------------
    def test_no_network_exits_cleanly_and_touches_nothing(self):
        r = self.run_script(
            "--network-timeout", "2",
            WALLY_NETWORK_HOSTS=f"127.0.0.1:{self.offline_port}")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("No network", r.stdout)
        self.assert_untouched()

    def test_dry_run_downloads_but_changes_nothing(self):
        r = self.run_script("--dry-run")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("DRY RUN", r.stdout)
        self.assert_untouched()
        self.assertFalse((self.wall_dir / ".seen_hashes").exists())

    def test_dry_run_without_network(self):
        r = self.run_script(
            "--dry-run", "--network-timeout", "1",
            WALLY_NETWORK_HOSTS=f"127.0.0.1:{self.offline_port}")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assert_untouched()

    # -- failure modes keep the old wallpaper ------------------------------
    def test_gallery_dl_failure_keeps_wallpaper(self):
        r = self.run_script(FAKE_GALLERY_MODE="fail")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assert_untouched()

    def test_garbage_download_keeps_wallpaper(self):
        r = self.run_script(FAKE_GALLERY_MODE="garbage")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assert_untouched()

    def test_hanging_gallery_dl_is_killed(self):
        code = (
            "import sys, runpy; sys.argv=['wallpaper.py'];"
            "import wallpaper; wallpaper.GALLERY_DL_TIMEOUT=1;"
            "wallpaper.MAX_ATTEMPTS=1; sys.exit(wallpaper.main([]))"
        )
        r = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                           env={**self.env, "FAKE_GALLERY_MODE": "hang"},
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("timed out", r.stdout)
        self.assert_untouched()

    # -- success path ------------------------------------------------------
    def test_success_replaces_atomically_and_calls_gsettings_safely(self):
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotEqual(self.wallpaper.read_bytes(), self.original)
        with Image.open(self.wallpaper) as img:
            self.assertEqual(img.size, (1920, 1080))
        self.assertEqual(list(self.wall_dir.glob(".wallpaper-*")), [])
        self.assertFalse((self.cache_dir / "tmp").exists())
        sets = [c for c in self.gsettings_calls() if c.startswith("set")]
        self.assertTrue(sets)
        for call in sets:
            self.assertNotIn("''", call)
            if "picture-uri" in call:
                uri = call.split()[-1]
                self.assertTrue(uri.startswith("file:///"), call)
                self.assertTrue(uri.endswith("wallpaper.jpg"), call)
        self.assertTrue((self.wall_dir / ".seen_hashes").read_text().strip())

    # -- guards ------------------------------------------------------------
    def test_apply_refuses_missing_or_empty_file(self):
        code = (
            "import wallpaper, sys;"
            "print(wallpaper.apply_wallpaper())"
        )
        self.wallpaper.write_bytes(b"")
        r = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                           env=self.env, capture_output=True, text=True)
        self.assertEqual(r.stdout.strip().splitlines()[-1], "False")
        self.wallpaper.unlink()
        r = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                           env=self.env, capture_output=True, text=True)
        self.assertEqual(r.stdout.strip().splitlines()[-1], "False")
        self.assertEqual(self.gsettings_calls(), [])

    def test_apply_refuses_directory(self):
        self.wallpaper.unlink()
        self.wallpaper.mkdir()
        r = subprocess.run(
            [sys.executable, "-c",
             "import wallpaper;print(wallpaper.apply_wallpaper())"],
            cwd=ROOT, env=self.env, capture_output=True, text=True)
        self.assertEqual(r.stdout.strip().splitlines()[-1], "False")
        self.assertEqual(self.gsettings_calls(), [])

    def test_second_instance_exits_while_lock_held(self):
        import fcntl
        self.cache_dir.mkdir(parents=True)
        with open(self.cache_dir / "wally.lock", "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Another instance", r.stdout)
        self.assert_untouched()

    def test_missing_gallery_dl_is_a_real_error(self):
        r = self.run_script(PATH="/nonexistent-dir")
        # python itself must still be found, so run it by absolute path
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
