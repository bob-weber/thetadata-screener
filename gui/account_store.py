"""Per-account file storage for the portfolio tracker.

Each brokerage account keeps its own pair of JSON files:

    my_option_events_<acct>.json   — wheel-cycle cash events (Positions tab)
    gains_history_<acct>.json      — deposits / balances / trades (P&L + Weekly RoR)

``<acct>`` is the TOS account number, sanitised for the filesystem. Several
accounts can therefore coexist side by side; importing a statement routes to its
own account's bucket instead of overwriting another account's history.

Older single-account files (no account in the name) are migrated into a bucket on
first run.
"""

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

_EVENTS_PREFIX = "my_option_events_"
_GAINS_PREFIX  = "gains_history_"
_SUFFIX        = ".json"

# Pre-existing single-account files, migrated into per-account buckets once.
_LEGACY_EVENTS = Path("my_option_events.json")
_LEGACY_GAINS  = Path("gains_history.json")
_LEGACY_CACHES = (Path("my_option_positions.json"),
                  Path("my_stock_positions.json"),
                  Path("my_option_superseded.json"))

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
DEFAULT_ACCT = "default"


def safe_token(acct: str) -> str:
    """Filesystem-safe token for an account id (empty → 'default')."""
    return _UNSAFE.sub("_", (acct or "").strip()) or DEFAULT_ACCT


def events_path(acct: str) -> Path:
    return Path(f"{_EVENTS_PREFIX}{safe_token(acct)}{_SUFFIX}")


def gains_path(acct: str) -> Path:
    return Path(f"{_GAINS_PREFIX}{safe_token(acct)}{_SUFFIX}")


def _account_in(p: Path) -> str:
    """The account id recorded in an events file, falling back to its filename token."""
    try:
        acct = (json.loads(p.read_text()).get("account") or "").strip()
    except Exception:
        acct = ""
    return acct or p.stem[len(_EVENTS_PREFIX):]


def list_accounts() -> list[str]:
    """All known account ids, most-recently-used first."""
    paths = sorted(Path(".").glob(f"{_EVENTS_PREFIX}*{_SUFFIX}"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    out, seen = [], set()
    for p in paths:
        acct = _account_in(p)
        if acct and acct not in seen:
            seen.add(acct)
            out.append(acct)
    return out


def write_json(path: Path, data) -> None:
    """Write ``data`` to ``path`` all or nothing.

    The account files are the only copy of the imported history. Writing in
    place means a crash or full disk mid-write leaves a truncated file, which
    then loads as an empty account — and the next import overwrites it. Writing
    beside it and swapping it in leaves the old file whole on any failure.
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def keep_unreadable(path: Path) -> Path:
    """Copy a file that won't parse aside before anything can overwrite it."""
    backup = path.with_name(f"{path.name}.unreadable-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(path, backup)
    return backup


def _set_aside(path: Path) -> None:
    """Rename a legacy file out of the way instead of deleting it."""
    path.rename(path.with_name(f"{path.name}.migrated-{datetime.now():%Y%m%d-%H%M%S}"))


def migrate_legacy() -> None:
    """Move pre-existing single-account files into per-account buckets (once)."""
    if not _LEGACY_EVENTS.exists():
        return
    try:
        acct = (json.loads(_LEGACY_EVENTS.read_text()).get("account") or "").strip()
    except Exception:
        acct = ""
    acct = acct or DEFAULT_ACCT

    # A per-account file that already exists wins, but the legacy one is kept
    # (renamed), not deleted: it may hold history the bucket doesn't.
    dest = events_path(acct)
    if dest.exists():
        _set_aside(_LEGACY_EVENTS)
    else:
        _LEGACY_EVENTS.rename(dest)

    # The gains file has no account of its own; it belongs to the same account.
    if _LEGACY_GAINS.exists():
        gdest = gains_path(acct)
        if gdest.exists():
            _set_aside(_LEGACY_GAINS)
        else:
            _LEGACY_GAINS.rename(gdest)

    # Lifecycle caches are rebuilt from events; drop the legacy copies.
    for p in _LEGACY_CACHES:
        p.unlink(missing_ok=True)
