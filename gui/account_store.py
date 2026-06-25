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
import re
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


def migrate_legacy() -> None:
    """Move pre-existing single-account files into per-account buckets (once)."""
    if not _LEGACY_EVENTS.exists():
        return
    try:
        acct = (json.loads(_LEGACY_EVENTS.read_text()).get("account") or "").strip()
    except Exception:
        acct = ""
    acct = acct or DEFAULT_ACCT

    dest = events_path(acct)
    if dest.exists():
        _LEGACY_EVENTS.unlink(missing_ok=True)
    else:
        _LEGACY_EVENTS.rename(dest)

    # The gains file has no account of its own; it belongs to the same account.
    if _LEGACY_GAINS.exists():
        gdest = gains_path(acct)
        if gdest.exists():
            _LEGACY_GAINS.unlink(missing_ok=True)
        else:
            _LEGACY_GAINS.rename(gdest)

    # Lifecycle caches are rebuilt from events; drop the legacy copies.
    for p in _LEGACY_CACHES:
        p.unlink(missing_ok=True)
