from __future__ import annotations

import csv
import os
import tempfile
from pathlib import Path

from .views import View

FILE_NAMES = {
    "Companies": "companies.csv",
    "Jobs": "jobs.csv",
    "NewJobs": "new_jobs.csv",
    "LocationUnclear": "location_unclear.csv",
    "Runs": "runs.csv",
}


def write_csv(view: View, out_dir: str | Path) -> Path:
    """Atomic write (temp file + replace) so readers never see a half-written export."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / FILE_NAMES.get(view.name, f"{view.name.lower()}.csv")
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".csv", dir=out_dir)
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh, quoting=csv.QUOTE_MINIMAL)
            w.writerow(view.header)
            w.writerows(view.rows)
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return dest
