# Wally

Wally is an automated Python script designed to periodically refresh your desktop wallpaper on Linux/GNOME systems. It fetches high-quality 16:9 images from Pinterest based on curated search queries, filtering for resolution and aspect ratio before applying them.

---

## Features

* Pinterest wallpaper search
* Automatic wallpaper download
* 16:9 wallpaper filtering
* Desktop wallpaper automation
* Startup automation (systemd user timer, never blocks login)
* Random wallpaper selection
* Linux / GNOME support

---

## Demo

The application follows a straightforward workflow upon execution:

```
Boot
↓
Search Pinterest
↓
Download wallpapers
↓
Filter 16:9
↓
Random selection
↓
Apply wallpaper
```

---

## Folder Structure

The project relies on the following layout to operate correctly:

```
wally/
│
├── wallpaper.py
├── systemd/
│   ├── wally.service     # one-shot job (template, paths filled in by install.sh)
│   └── wally.timer       # starts it 60s after login, retries every 5 min
├── scripts/
│   ├── install.sh        # user-level install, no sudo
│   └── remove-old.sh     # removes the old service + autostart duplicates
├── tests/test_wallpaper.py
├── requirements.txt
├── README.md
├── LICENSE
├── .gitignore
├── wallpaper/
│   ├── wallpaper.jpg     # The currently active wallpaper
│   ├── .seen_hashes      # Hashes of recently shown images to avoid repeats
│   └── .last_query       # The last category searched
```

---

## Installation

To get Wally running on your system, follow these steps:

1. **Verify Python version:** 
   Ensure you have Python 3 installed.
   ```bash
   python3 --version
   ```

2. **Clone the repository:**
   ```bash
   git clone https://github.com/Jaswanth-dev-69/wally.git
   cd wally
   ```

3. **Install dependencies:**
   Wally requires `gallery-dl` for downloading images and `Pillow` for image validation.
   ```bash
   pip3 install -r requirements.txt
   ```

---

## Usage

Run it by hand to change the wallpaper right now:

```bash
python3 wallpaper.py            # real run
python3 wallpaper.py --dry-run  # wait for network, download, validate; change nothing
```

Safety behaviour (all of it is why login is no longer affected):

* Only one instance runs at a time (`flock` on `~/.cache/wally/wally.lock`).
* Waits for the network for at most 30 s, then exits **0** quietly and leaves
  the current wallpaper alone. The timer retries 5 minutes later.
* Downloads go to `~/.cache/wally/tmp` (not `/tmp`), `gallery-dl` runs under
  `timeout`, and the scratch dir is deleted afterwards.
* The new image is validated, written to a temp file in `wallpaper/` and moved
  over `wallpaper.jpg` with an atomic rename. On any failure the old
  `wallpaper.jpg` stays as it was.
* `gsettings` is only called after checking `wallpaper.jpg` exists, is
  non-empty and decodes as an image. It is never set to `""`.

---

## Auto Start (systemd user timer, no sudo)

Wally starts through **one** mechanism: a systemd *user* timer. It fires 60 s
after login, in the background, so it can never hold up the desktop.

### 1. Remove the old duplicate launchers

Either run the helper:

```bash
cd ~/Documents/sp/wally
git pull            # (or switch to the branch with these changes)
./scripts/remove-old.sh
```

or do it by hand:

```bash
# old systemd user service
systemctl --user disable --now pinterest-wallpaper.service
rm -f ~/.config/systemd/user/pinterest-wallpaper.service
rm -f ~/.config/systemd/user/*.wants/pinterest-wallpaper.service
systemctl --user daemon-reload
systemctl --user reset-failed

# old autostart entry (find it, then delete it)
grep -liE 'pinterest|wally|wallpaper\.py' ~/.config/autostart/*.desktop
rm -f ~/.config/autostart/pinterest-wallpaper.desktop
# ...and delete the "Wally Wallpaper" entry created in Startup Applications, if any
```

If `systemctl --user cat pinterest-wallpaper.service` shows the unit lives under
`/etc/systemd/user/` or `/etc/xdg/autostart/`, those are system-wide and would
need `sudo rm` - the helper only tells you about them.

### 2. Install and enable the timer

```bash
cd ~/Documents/sp/wally
pip3 install --user -r requirements.txt
./scripts/install.sh
systemctl --user start wally.timer     # run once now, without rebooting
```

`install.sh` writes `~/.config/systemd/user/wally.{service,timer}` (with the
real project path filled in) and runs `systemctl --user enable wally.timer`.

### Checking on it

```bash
systemctl --user list-timers wally.timer
journalctl --user -u wally.service -b --no-pager
systemctl --user status wally.service
```

To uninstall: `systemctl --user disable --now wally.timer && rm ~/.config/systemd/user/wally.{service,timer} && systemctl --user daemon-reload`.

### Verifying the black screen is gone after a reboot

```bash
journalctl -b | grep -E "GNOME Shell started|session opened for user altf4"
```

The gap between those two lines should be a few seconds. Then confirm Wally
did its work *after* the desktop was up:

```bash
journalctl --user -b -u wally.service --no-pager | head -30
journalctl -b | grep -c "Failed to load background"     # expect 0
```

If the black screen is still there, Wally is not the cause; look at what
the login actually waits on:

```bash
systemd-analyze --user critical-chain
systemd-analyze blame | head
journalctl -b -p warning --no-pager | head -60
```

---

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The tests use fake `gallery-dl`/`gsettings` programs, so no network or GNOME
session is needed. They cover: no network (exits 0, nothing touched), dry run,
gallery-dl failure/garbage/hang, atomic replace, never calling `gsettings`
with a missing/empty/directory path, and the single-instance lock.

---

## Configuration

You can tweak the behavior of Wally by editing `wallpaper.py`:

* **Search keywords:** Update the `SEARCH_QUERIES` list with your preferred Pinterest search terms.
* **Wallpaper directory:** The `WALLPAPER_DIR` variable dictates where downloaded images and metadata are stored.
* **Resolution filtering:** Modify `MIN_WIDTH` (default 1920) for minimum horizontal resolution.
* **Aspect ratio filtering:** Adjust `TARGET_RATIO` (default 16/9) and `RATIO_TOLERANCE` to ensure wallpapers perfectly fit your screen.

---

## Dependencies

* **Pillow (`PIL`)**: Used locally as a strict safety net to inspect image properties. It guarantees that the downloaded image meets the exact aspect ratio and minimum width requirements before it modifies your desktop.
* **gallery-dl**: Powers the actual Pinterest searching and downloading. It handles the API/scraping overhead and provides server-side metadata filtering to optimize network usage.

---

## Future Improvements

No future updates or improvements are planned for this project.

---

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
