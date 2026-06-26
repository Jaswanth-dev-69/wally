# Wally

Wally is an automated Python script designed to periodically refresh your desktop wallpaper on Linux/GNOME systems. It fetches high-quality 16:9 images from Pinterest based on curated search queries, filtering for resolution and aspect ratio before applying them.

---

## Features

* Pinterest wallpaper search
* Automatic wallpaper download
* 16:9 wallpaper filtering
* Desktop wallpaper automation
* Startup automation
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
├── run.sh
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

You can run the script manually to change the wallpaper immediately:

```bash
python3 wallpaper.py
```

Or you can use the provided bash script which includes a startup delay:

```bash
./run.sh
```

---

## Auto Start

To have your wallpaper change automatically on GNOME/Zorin OS upon login:

1. Open **Startup Applications** on your system.
2. Click **Add**.
3. Fill in the details:
   - **Name**: Wally Wallpaper
   - **Command**: `/path/to/wally/run.sh`
   - **Comment**: Automatically updates desktop wallpaper.
4. Click **Save**.

The `run.sh` script includes a short delay (`sleep 20`) to ensure your network is fully connected before it attempts to download a new wallpaper.

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
