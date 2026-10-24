"""EPUB to KEPUB conversion with the kepubify binary."""

import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def kepubify_available(binary: str) -> bool:
    return shutil.which(binary) is not None


def kepubify(binary: str, source: Path, target: Path) -> bool:
    """Convert source to target. Returns False (and logs) if kepubify fails."""
    try:
        result = subprocess.run(
            # No dummy title page: it costs a tap on every (single-file) article.
            [binary, "--no-add-dummy-titlepage", "--output", str(target), str(source)],
            capture_output=True, text=True, timeout=120, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("kepubify could not run: %r", exc)
        return False
    if result.returncode != 0 or not target.exists():
        log.warning("kepubify failed (%s): %s", result.returncode, (result.stderr or result.stdout).strip()[:500])
        return False
    return True
