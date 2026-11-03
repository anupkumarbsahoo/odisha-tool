"""Appends one row per automation run to logs/log.csv."""
import csv
from datetime import datetime
from pathlib import Path

LOG_FILE = Path(__file__).resolve().parent / "logs" / "log.csv"
COLUMNS = ["timestamp", "action", "registration_number", "username", "document", "status",
           "village_input", "village_match", "village_score", "filled", "mismatches",
           "skipped", "site_messages", "error", "duration_s"]


def _upgrade_header():
    """Rewrite an older log whose columns differ, keeping its rows under the matching headings."""
    with LOG_FILE.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames == COLUMNS:
            return
        rows = list(reader)
    with LOG_FILE.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def log_run(row):
    LOG_FILE.parent.mkdir(exist_ok=True)
    new_file = not LOG_FILE.exists()
    if not new_file:
        _upgrade_header()
    row = {"timestamp": datetime.now().isoformat(timespec="seconds"), **row}
    with LOG_FILE.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        if new_file:
            writer.writeheader()
        writer.writerow(row)
