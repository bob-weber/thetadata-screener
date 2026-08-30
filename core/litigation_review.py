"""Automated litigation review from SEC filings.

Runs as part of LSO Analysis, for exactly the symbols being analysed. Litigation
status is only actionable when you are about to write a put on a name, and it
decays — so it is fetched on demand for candidates rather than pre-computed
across a universe, where most entries would go stale unused.

``litigation.json`` is the cache. An entry inside its ``review_by`` window is
reused untouched; a missing or stale one is re-read from the company's latest
10-Q or 10-K. That makes a repeat analysis free and a quarterly refresh
automatic.

What is detected here is *presence and shape* — a named, broken-out matter, the
securities-class-action pattern, the credibility overlay. Whether a case
threatens the franchise rather than merely costing money stays a human call, so
machine verdicts are marked ``auto`` and never overwrite a confirmed one
downwards. See docs/grading.md for the rubric they implement.
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path

import requests

from core.lso_analyzer import (LITIGATION_FILE, _LITIGATION_REVIEW_DAYS,
                               _litigation_state, load_litigation)

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
CIK_CACHE_FILE  = "edgar_cik_cache.json"     # matches *_cache.json in .gitignore

# SEC asks for a contact address and caps traffic at 10 requests/second.
_UA         = {"User-Agent": "lso-tools/1.0 contact@example.com"}
_SEC_WORKERS    = 4
_SEC_START_RATE = 6

# Language that marks a real, broken-out matter rather than the ordinary-course
# paragraph every filer carries.
_CAPTION_RE   = re.compile(
    r"\b[A-Z][A-Za-z.\-]+\s+v\.?s?\.\s+[A-Z]"          # Brownback v. AppLovin
    r"|\bIn re\b[^.]{0,80}?(Litigation|Antitrust|Securities)",  # In re … Litigation
    re.UNICODE)
# Broken-out headings a filer only writes when a matter is not ordinary course.
_NAMED_HEADING_RE = re.compile(
    r"Securities Litigation|Shareholder Derivative|Antitrust (Litigation|Matters)"
    r"|Government Investigation|Patent Litigation|Opioid|Talc", re.I)
_SECURITIES_RE = re.compile(
    r"10\(b\)|10b-5|putative class action|securities class action", re.I)
_DERIVATIVE_RE = re.compile(r"derivative (complaint|action|suit)", re.I)
# The credibility overlay. Searched only inside the legal-proceedings section:
# "restatement" and "material weakness" appear as risk-factor and auditor
# boilerplate in almost every filing, so a whole-document search is meaningless.
_CREDIBILITY_RE = re.compile(
    r"subpoena|civil investigative demand|formal order of investigation"
    r"|wells notice|restatement of (our|the) (previously issued )?financial"
    r"|identified a material weakness", re.I)
_UNBOUNDED_RE = re.compile(
    r"cannot (reasonably )?(be )?estimate|unable to estimate", re.I)
_ORDINARY_RE  = re.compile(r"ordinary course", re.I)

# A section this short carries no named matter — it is the boilerplate sentence.
_BOILERPLATE_CHARS = 700


def _get(url: str, timeout: int = 60) -> requests.Response:
    r = requests.get(url, headers=_UA, timeout=timeout)
    r.raise_for_status()
    return r


def _text_of(html: str) -> str:
    """Filing HTML → flat text."""
    import html as html_mod
    txt = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", html_mod.unescape(txt))


def load_cik_map(cache_file: str | Path = CIK_CACHE_FILE) -> dict[str, int]:
    """Ticker → CIK, cached on disk (the EDGAR list changes slowly)."""
    p = Path(cache_file)
    if p.exists():
        try:
            return {k: int(v) for k, v in json.loads(p.read_text()).items()}
        except Exception:
            pass
    data = _get(SEC_TICKERS_URL, timeout=30).json()
    fields, rows = data["fields"], data["data"]
    ti, ci = fields.index("ticker"), fields.index("cik")
    out = {str(r[ti]).strip().upper(): int(r[ci]) for r in rows if r[ti]}
    try:
        p.write_text(json.dumps(out))
    except Exception:
        pass
    return out


def _latest_filing(cik: int) -> tuple[str, str, str] | None:
    """``(form, filing_date, document_url)`` for the newest 10-Q or 10-K."""
    s = _get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", timeout=30).json()
    rec = s.get("filings", {}).get("recent", {})
    best = None
    for form, filed, acc, doc in zip(rec.get("form", []), rec.get("filingDate", []),
                                     rec.get("accessionNumber", []),
                                     rec.get("primaryDocument", [])):
        if form not in ("10-Q", "10-K") or not doc:
            continue
        if best is None or filed > best[1]:
            url = (f"https://www.sec.gov/Archives/edgar/data/{cik}/"
                   f"{acc.replace('-', '')}/{doc}")
            best = (form, filed, url)
    return best


def extract_legal_section(txt: str, form: str) -> str:
    """The Legal Proceedings section, skipping the table-of-contents match.

    A 10-Q carries it at Part II Item 1, a 10-K at Item 3. Both names also occur
    in the contents listing near the top, so the *last* match that is followed by
    real prose is the section itself.
    """
    if form == "10-K":
        start_re, end_re = r"ITEM\s*3\.?\s*[—–-]?\s*LEGAL PROCEEDINGS", r"ITEM\s*4\.?"
    else:
        start_re, end_re = r"ITEM\s*1\.?\s*[—–-]?\s*LEGAL PROCEEDINGS", r"ITEM\s*1A\.?"
    best = ""
    for m in re.finditer(start_re, txt, re.I):
        tail = txt[m.start():m.start() + 60000]
        e = re.search(end_re, tail[40:], re.I)
        section = tail[:e.start() + 40] if e else tail[:8000]
        if len(section) > len(best):
            best = section
    return best.strip()


_NOTE_ANCHOR_RE = re.compile(
    r"Loss Contingencies|Legal Proceedings|Commitments and Contingencies"
    r"|Legal Matters", re.I)


def extract_legal_corpus(txt: str, form: str) -> str:
    """Item 1/Item 3 plus the contingencies note windows.

    A 10-Q's Item 1 is often one line pointing at the notes ("see Note 12"), so
    classifying on Item 1 alone reads a live case as boilerplate. The note text
    is where the captions and the accrual actually are.
    """
    parts = [extract_legal_section(txt, form)]
    for m in _NOTE_ANCHOR_RE.finditer(txt):
        parts.append(txt[m.start():m.start() + 4000])
    seen, out = set(), []
    for part in parts:
        key = part[:120]
        if part and key not in seen:
            seen.add(key)
            out.append(part)
    return "\n".join(out)


def classify(section: str) -> dict:
    """Propose a severity from one Legal Proceedings section, with evidence."""
    ev: list[str] = []
    if not section:
        return {"severity": None, "confidence": "low", "evidence": ["no section found"]}

    caption     = bool(_CAPTION_RE.search(section))
    heading     = bool(_NAMED_HEADING_RE.search(section))
    securities  = bool(_SECURITIES_RE.search(section))
    derivative  = bool(_DERIVATIVE_RE.search(section))
    credibility = bool(_CREDIBILITY_RE.search(section))
    unbounded   = bool(_UNBOUNDED_RE.search(section))

    if caption:
        ev.append("named case caption")
    if heading:
        ev.append("broken-out legal heading")
    if securities:
        ev.append("securities class action (10b-5)")
    if derivative:
        ev.append("shareholder derivative suit")
    if credibility:
        ev.append("regulator/restatement language")
    if unbounded:
        ev.append("loss not estimable")

    if securities and credibility:
        # The short-attack pattern: the risk is the accounting, not the case.
        return {"severity": "existential", "confidence": "medium", "evidence": ev}

    # Presence of litigation is not the signal — every large filer has a docket,
    # and a standing "Antitrust Matters" or "Patent Litigation" heading is
    # baseline for mega-cap tech and pharma, not deviation from it. So a heading
    # on its own is not enough to size a position down: only a specific case
    # caption, a securities class action or a derivative suit acts. A heading
    # with nothing behind it goes to `review` for a human read.
    if securities or derivative or caption:
        return {"severity": "overhang",
                "confidence": "medium" if (securities or caption) else "low",
                "evidence": ev}
    if heading:
        return {"severity": "review", "confidence": "low",
                "evidence": ev + ["heading only — no caption or case-type signal"]}
    if len(section) <= _BOILERPLATE_CHARS or _ORDINARY_RE.search(section):
        return {"severity": "clear", "confidence": "medium",
                "evidence": ev + ["ordinary-course language only"]}
    # Substantial legal text with nothing decisive in it. Refusing to guess is
    # the point: defaulting to overhang here would cap the tier on most of the
    # S&P, and defaulting to clear would hide a real matter behind bad parsing.
    return {"severity": "review", "confidence": "low",
            "evidence": ev + ["legal text present but no decisive signal"]}


def review_symbol(symbol: str, cik_map: dict[str, int] | None = None) -> dict | None:
    """Fetch and classify one symbol. Returns a litigation.json entry, or None."""
    cik_map = cik_map if cik_map is not None else load_cik_map()
    cik = cik_map.get(symbol.strip().upper())
    if cik is None:
        return None
    latest = _latest_filing(cik)
    if latest is None:
        return None
    form, filed, url = latest
    section = extract_legal_corpus(_text_of(_get(url).text), form)
    verdict = classify(section)

    today = date.today()
    entry = {
        "asof":      today.isoformat(),
        "review_by": (today + timedelta(days=_LITIGATION_REVIEW_DAYS)).isoformat(),
        "auto":      True,
        "confidence": verdict["confidence"],
        "evidence":  verdict["evidence"],
        "filing":    f"{form} {filed}",
        "source":    url,
    }
    if verdict["severity"]:
        entry["severity"] = verdict["severity"]
        entry["note"] = "; ".join(verdict["evidence"])
    else:
        # Recorded rather than omitted: "checked, nothing found" is a result, and
        # it stops the symbol being re-fetched on every analysis for 100 days.
        entry["severity"] = "clear"
        entry["note"] = "no broken-out matter in the latest filing"
    return entry


def _load_store(litigation_file: str | Path) -> dict:
    """Read the store from the given path (not the module default)."""
    if str(litigation_file) == str(LITIGATION_FILE):
        return load_litigation()
    try:
        raw = json.loads(Path(litigation_file).read_text())
    except Exception:
        return {}
    return {str(k).strip().upper(): dict(v) if isinstance(v, dict)
            else {"severity": v} for k, v in raw.items()}


def _needs_review(entry: dict | None, today: date) -> bool:
    if not entry:
        return True
    return _litigation_state(entry, today)["stale"]


def _fetch_all(todo, call_one, on_progress=None, stop_flag=None, on_log=None) -> dict:
    """Run ``call_one`` over ``todo`` concurrently, paced under SEC's 10 req/s.

    Deliberately not ``screener._concurrent_fetch``: that one is wired to Schwab
    (auth-error detection, and a circuit breaker that reports a hard block as
    "Schwab is blocking requests"), which would be a misleading error for an
    EDGAR failure. The rate limiter itself is generic, so that is reused.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from core.screener import _RateLimiter

    limiter = _RateLimiter(_SEC_START_RATE)
    results: dict = {}

    def worker(key):
        for attempt in range(4):
            limiter.pace()
            try:
                value = call_one(key)
                limiter.recover()
                return key, value
            except Exception as e:
                status = getattr(getattr(e, "response", None), "status_code", None)
                if status in (429, 403):        # SEC throttles with 403 as well as 429
                    limiter.hit(attempt)
                    continue
                if on_log and attempt == 0:
                    on_log(f"  {key}: filing check failed — {e}")
                return key, None
        if on_log:
            on_log(f"  {key}: gave up after repeated SEC rate limits")
        return key, None

    done, total = 0, len(todo)
    with ThreadPoolExecutor(max_workers=_SEC_WORKERS) as pool:
        futures = [pool.submit(worker, k) for k in todo]
        for fut in as_completed(futures):
            if stop_flag and stop_flag():
                for f in futures:
                    f.cancel()
                return results
            key, value = fut.result()
            done += 1
            if value is not None:
                results[key] = value
            if on_progress:
                on_progress(done, total)
    return results


def refresh(symbols, on_log=None, on_progress=None, stop_flag=None,
            litigation_file: str | Path = LITIGATION_FILE) -> dict:
    """Bring ``litigation.json`` current for ``symbols``. Returns the store.

    Only missing or stale entries are fetched, so a repeat analysis costs nothing
    and a quarterly refresh happens by itself. A confirmed (human) entry is never
    downgraded by a machine verdict — a higher auto severity is applied, anything
    lower is recorded alongside as ``auto_severity`` for you to look at, since a
    parsing miss must not quietly un-gate a name you rejected on purpose.
    """
    today = date.today()
    store = dict(_load_store(litigation_file))
    wanted = [s.strip().upper() for s in dict.fromkeys(symbols) if s and s.strip()]
    todo   = [s for s in wanted if _needs_review(store.get(s), today)]
    if on_log:
        on_log(f"Litigation review: {len(todo)} of {len(wanted)} symbols need a "
               f"filing check ({len(wanted) - len(todo)} cached and current).")
    if not todo:
        return store

    try:
        cik_map = load_cik_map()
    except Exception as e:
        if on_log:
            on_log(f"Litigation review skipped — could not load the EDGAR "
                   f"ticker map: {e}")
        return store

    results = _fetch_all(todo, lambda s: review_symbol(s, cik_map),
                         on_progress=on_progress, stop_flag=stop_flag, on_log=on_log)

    changed = raised = 0
    for sym, entry in results.items():
        old = store.get(sym) or {}
        if old.get("confirmed"):
            if _SEVERITY_ORDER.get(entry["severity"], 0) > \
               _SEVERITY_ORDER.get(old.get("severity"), 0):
                store[sym] = {**old, **entry, "confirmed": False,
                              "note": f"auto-raised over confirmed "
                                      f"{old.get('severity')}: {entry['note']}"}
                raised += 1
            else:
                store[sym] = {**old, "auto_severity": entry["severity"],
                              "auto_asof": entry["asof"]}
            changed += 1
            continue
        store[sym] = entry
        changed += 1

    try:
        Path(litigation_file).write_text(json.dumps(store, indent=2, sort_keys=True))
    except Exception as e:
        if on_log:
            on_log(f"  Could not write {litigation_file}: {e}")
    if on_log:
        found = sum(1 for s in todo
                    if (store.get(s) or {}).get("severity") in ("overhang", "existential"))
        on_log(f"Litigation review: {changed} entries updated, {found} with a "
               f"matter on file"
               + (f", {raised} auto-raised over a confirmed entry" if raised else ""))
    return store


_SEVERITY_ORDER = {None: 0, "clear": 0, "overhang": 1, "existential": 2}


if __name__ == "__main__":
    import sys
    syms = [s.upper() for s in sys.argv[1:]]
    if not syms:
        print("usage: python -m core.litigation_review TICKER [TICKER ...]")
        raise SystemExit(2)
    store = refresh(syms, on_log=lambda m: print(m))
    for s in syms:
        print(json.dumps({s: store.get(s)}, indent=2))
