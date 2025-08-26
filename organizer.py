#!/usr/bin/env python3
"""
Bulk File Organizer
- Put a config.json beside this script.
- pip install watchdog
Run:
  python organizer.py --watch
  python organizer.py --once
"""

import os
import shutil
import time
import re
import json
import logging
import argparse
import threading
from datetime import datetime

# Optional import: watchdog
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    WATCHDOG_AVAILABLE = True
except Exception:
    WATCHDOG_AVAILABLE = False

# ---------- Utilities ----------
def load_config(path="config.json"):
    # default config
    default = {
        "watch_dir": os.path.expanduser("~/Downloads"),
        "recursive": False,
        "file_types": {
            "Images": [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".svg"],
            "Documents": [".pdf", ".docx", ".doc", ".txt", ".xlsx", ".pptx"],
            "Videos": [".mp4", ".mov", ".mkv", ".avi"],
            "Music": [".mp3", ".wav", ".flac"],
            "Archives": [".zip", ".rar", ".7z", ".tar.gz"],
            "Code": [".py", ".js", ".java", ".c", ".cpp", ".html", ".css"]
        },
        "regex_rules": [
            # {"pattern": "invoice", "folder": "Invoices"}
        ],
        "date_based": True,
        "date_field": "mtime",    # 'mtime' recommended (cross-platform)
        "date_format": "%Y-%m",
        "temp_extensions": [".crdownload", ".part", ".tmp"],
        "exclude_patterns": [],
        "dry_run": False,
        "wait_for_stable_seconds": 1,
        "stable_checks": 3,
        "log_file": "organizer.log"
    }
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            user = json.load(f)
            default.update(user)
    return default

def setup_logging(log_file):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_file, encoding="utf-8")
        ]
    )

def is_temporary(filename, config):
    lower = filename.lower()
    for ext in config.get("temp_extensions", []):
        if lower.endswith(ext):
            return True
    return False

def wait_until_stable(path, wait_sec=1, checks=3, timeout=60):
    """Return True when file size stable for `checks` checks. Waits up to timeout seconds."""
    start = time.time()
    stable = 0
    try:
        last_size = os.path.getsize(path)
    except OSError:
        return False
    while time.time() - start < timeout and stable < checks:
        time.sleep(wait_sec)
        try:
            size = os.path.getsize(path)
        except OSError:
            return False
        if size == last_size:
            stable += 1
        else:
            stable = 0
            last_size = size
    return stable >= checks

def unique_destination(dst_path):
    """If dst_path exists, append (1), (2), ... before extension."""
    if not os.path.exists(dst_path):
        return dst_path
    base, ext = os.path.splitext(dst_path)
    i = 1
    while True:
        candidate = f"{base} ({i}){ext}"
        if not os.path.exists(candidate):
            return candidate
        i += 1

# ---------- Core move logic ----------
def determine_target_folder(filename, config):
    fname = filename
    # regex rules first (more specific)
    for r in config.get("regex_rules", []):
        try:
            if re.search(r["pattern"], fname, re.IGNORECASE):
                return r["folder"]
        except Exception:
            continue
    # extension rules
    ext = os.path.splitext(filename)[1].lower()
    for folder, exts in config.get("file_types", {}).items():
        if ext in [e.lower() for e in exts]:
            return folder
    return "Others"

def date_subfolder(file_path, config):
    if not config.get("date_based"):
        return ""
    field = config.get("date_field", "mtime")
    if field == "ctime":
        ts = os.path.getctime(file_path)
    else:
        ts = os.path.getmtime(file_path)
    return datetime.fromtimestamp(ts).strftime(config.get("date_format", "%Y-%m"))

def move_file(file_path, config):
    if not os.path.isfile(file_path):
        return
    filename = os.path.basename(file_path)
    if filename.startswith("."):
        # ignore hidden files
        logging.debug("Ignoring hidden file: %s", filename)
        return
    if is_temporary(filename, config):
        logging.info("Skipping temporary file: %s", filename)
        return

    # Wait until file is stable (not being written)
    if not wait_until_stable(file_path, wait_sec=config.get("wait_for_stable_seconds",1),
                             checks=config.get("stable_checks",3), timeout=300):
        logging.warning("File not stable or accessible, skipping: %s", filename)
        return

    target_folder = determine_target_folder(filename, config)
    date_folder = date_subfolder(file_path, config)
    watch_dir = config["watch_dir"]

    if date_folder:
        final_dir = os.path.join(watch_dir, target_folder, date_folder)
    else:
        final_dir = os.path.join(watch_dir, target_folder)

    os.makedirs(final_dir, exist_ok=True)
    destination = os.path.join(final_dir, filename)
    destination = unique_destination(destination)

    if config.get("dry_run"):
        logging.info("[DRY RUN] Would move: %s -> %s", file_path, destination)
    else:
        try:
            shutil.move(file_path, destination)
            logging.info("Moved: %s -> %s", file_path, destination)
        except Exception as e:
            logging.error("Failed to move %s -> %s : %s", file_path, destination, e)

# ---------- Watchdog handler ----------
class OrganizerHandler(FileSystemEventHandler):
    def __init__(self, config):
        super().__init__()
        self.config = config

    def on_created(self, event):
        # spawn a thread so we don't block the observer
        if event.is_directory:
            return
        path = event.src_path
        # check excludes
        for pat in self.config.get("exclude_patterns", []):
            if re.search(pat, os.path.basename(path)):
                logging.info("Excluded by pattern: %s", path)
                return
        t = threading.Thread(target=move_file, args=(path, self.config), daemon=True)
        t.start()

# ---------- Main ----------
def organize_once(config):
    logging.info("Running one-time organization on %s", config["watch_dir"])
    for root, dirs, files in os.walk(config["watch_dir"]):
        for f in files:
            path = os.path.join(root, f)
            # skip if file is inside organized subfolders we created
            rel = os.path.relpath(path, config["watch_dir"])
            if os.path.sep in rel and rel.split(os.path.sep)[0] in config.get("file_types", {}):
                # it is already inside a top-level folder we use; skip
                continue
            move_file(path, config)
        if not config.get("recursive"):
            break

def monitor_forever(config):
    if not WATCHDOG_AVAILABLE:
        logging.error("watchdog package not available. Install with: pip install watchdog")
        return
    event_handler = OrganizerHandler(config)
    observer = Observer()
    observer.schedule(event_handler, config["watch_dir"], recursive=config.get("recursive", False))
    observer.start()
    logging.info("Monitoring %s (recursive=%s)...", config["watch_dir"], config.get("recursive", False))
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()

def main():
    parser = argparse.ArgumentParser(description="Bulk File Organizer")
    parser.add_argument("--config", "-c", default="config.json")
    parser.add_argument("--watch", action="store_true", help="Run as watcher (use watchdog).")
    parser.add_argument("--once", action="store_true", help="Run once and exit.")
    args = parser.parse_args()

    config = load_config(args.config)
    setup_logging(config.get("log_file", "organizer.log"))
    logging.info("Config loaded: %s", args.config)

    # validate watch_dir
    watch_dir = os.path.expanduser(config["watch_dir"])
    if not os.path.isdir(watch_dir):
        logging.error("Watch directory does not exist: %s", watch_dir)
        return
    config["watch_dir"] = watch_dir

    if args.once:
        organize_once(config)
    elif args.watch:
        monitor_forever(config)
    else:
        # default: run once
        organize_once(config)

if __name__ == "__main__":
    main()
