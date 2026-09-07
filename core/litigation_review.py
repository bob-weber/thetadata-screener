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
                               _litigation_state, _money_value, load_litigation)

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
# "putative class action" on its own says nothing about the claim: LUV's is a
# wage-and-hour case, and pairing that with the credibility overlay hard-gated
# the symbol to grade F. Only a securities-flavoured class action belongs here.
_SECURITIES_RE = re.compile(
    r"10\(b\)|10b-5|securities class action"
    r"|putative (securities )?class action(?=[^.]{0,200}?"
    r"(securities|Exchange Act|shareholder|stockholder|investor))"
    r"|(securities|Exchange Act|shareholder|stockholder|investor)"
    r"[^.]{0,200}?putative class action", re.I)
_DERIVATIVE_RE = re.compile(r"derivative (complaint|action|suit)", re.I)
# The credibility overlay: is the *accounting* under question, or merely the
# conduct? Only the first is the short-attack pattern, where the risk is that
# every other input on the page is wrong too. Searched inside the legal corpus
# only — "restatement" and "material weakness" are risk-factor and auditor
# boilerplate in almost every filing, so a whole-document search is meaningless.
#
# Two tiers, because the words do not separate cleanly on their own. A
# restatement says the books were wrong; a subpoena says somebody wants
# documents, and every kind of litigation produces those. PayPal graded F on
# "Civil Investigative Demand" alone — an FTC question about merchant
# onboarding, in a filing with no restatement, no material weakness and no SEC
# or DOJ matter anywhere in it.

# Tier 1 — the books themselves. Each is either an SEC enforcement term of art
# or a direct statement that the financials cannot be relied on. No corroboration
# needed: nobody writes these about an ordinary commercial dispute.
_ACCOUNTING_RE = re.compile(
    r"restatement of (our|the) (previously issued )?financial"
    r"|restated? (our|the) (previously issued )?(consolidated )?financial"
    r"|non-?reliance on (our|the|previously|its)"
    r"|identified a material weakness"
    r"|material weakness(es)? in (our|the) internal control"
    r"|wells notice|formal order of (private )?investigation"
    r"|accounting irregularit|audit committee('s)? (internal )?investigation"
    r"|(resignation|dismissal) of (our|the) (independent )?"
    r"(registered public )?account", re.I)

# Tier 2 — an investigative demand, which is a credibility signal only when a
# disclosure regulator is asking *about the numbers*. The authority alone is not
# enough: the SEC and the DOJ investigate a great deal that has nothing to do
# with the books, and on a ten-filing sample every single demand-based hit was
# one of those — FCPA exposure (BSX), routine healthcare investigations (CVS), a
# False Claims Act cybersecurity case (LUNR), the HB6 bribery scandal (VST),
# anti-money-laundering in money transfer (WMT). Real, none of them a reason to
# distrust the financial statements. So the subject matter has to be named too.
# Uppercase-only for the abbreviations: a case-insensitive \bSEC\b also matches
# "Sec. 10(b)".
_DEMAND_RE = re.compile(
    r"subpoena|civil investigative demand|formal investigation"
    r"|investigative demand", re.I)
_ACCOUNTING_AUTHORITY_RE = re.compile(
    r"Securities and Exchange Commission|(?-i:\bSEC\b)|Department of Justice"
    r"|(?-i:\bDOJ\b)|U\.?S\.? Attorney|grand jury"
    r"|Public Company Accounting Oversight|(?-i:\bPCAOB\b)", re.I)
# What the demand has to be about. Deliberately not bare "financial statements":
# "NOTES TO CONDENSED CONSOLIDATED FINANCIAL STATEMENTS" is a page header that
# lands in the corpus at every page break, so it would match almost anywhere.
_ACCOUNTING_SUBJECT_RE = re.compile(
    r"revenue recognition|financial reporting|internal control|disclosure control"
    r"|accounting (practice|treatment|polic|irregular|error|method)"
    r"|restat|material (misstatement|weakness)|non-?GAAP|audit committee"
    r"|improperly (recognized|recorded|accounted|stated)"
    r"|books and records provision|earnings (misstat|manipulat)", re.I)

# Conduct and competition regulators. A demand from one of these is about how the
# business behaves, not about whether its numbers are real.
_CONDUCT_AUTHORITY_RE = re.compile(
    r"Federal Trade Commission|(?-i:\bFTC\b)"
    r"|Consumer Financial Protection|(?-i:\bCFPB\b)"
    r"|Federal Cartel Office|(?-i:\bFCO\b)"
    r"|Financial Conduct Authority|(?-i:\bFCA\b)"
    r"|Attorneys? General|(?-i:\bAGs?\b)"
    r"|Competition (and Markets )?Authority|(?-i:\bCMA\b)"
    r"|European Commission|Environmental Protection|(?-i:\bEPA\b)"
    r"|Food and Drug|(?-i:\bFDA\b)|Occupational Safety"
    r"|Equal Employment|Department of Labor|state regulators?", re.I)


def _credibility_evidence(section: str, window: int = 400) -> str | None:
    """The accounting-integrity signal in ``section``, or None for conduct only.

    A tier-1 phrase stands alone. A demand has to clear three tests: a disclosure
    regulator is named, the *nearest* named authority is that one rather than a
    conduct regulator (filers group their regulatory matters into one paragraph,
    so an SEC mention three matters away must not launder an FTC demand), and the
    accounting is what is being asked about. All three, because any two of them
    are satisfied by an ordinary FCPA or False Claims Act matter.
    """
    m = _ACCOUNTING_RE.search(section)
    if m:
        return " ".join(m.group(0).split()).lower()

    for m in _DEMAND_RE.finditer(section):
        lo = max(0, m.start() - window)
        near = section[lo:m.end() + window]
        here = m.start() - lo
        acct = min((abs(a.start() - here) for a in
                    _ACCOUNTING_AUTHORITY_RE.finditer(near)), default=None)
        if acct is None:
            continue
        cond = min((abs(c.start() - here) for c in
                    _CONDUCT_AUTHORITY_RE.finditer(near)), default=None)
        if cond is not None and cond <= acct:
            continue
        subject = _ACCOUNTING_SUBJECT_RE.search(near)
        if not subject:
            continue
        return (f"{' '.join(m.group(0).split()).lower()} from a disclosure "
                f"regulator, re: {' '.join(subject.group(0).split()).lower()}")
    return None
_UNBOUNDED_RE = re.compile(
    r"cannot (reasonably )?(be )?estimate|unable to estimate", re.I)
_ORDINARY_RE  = re.compile(r"ordinary course", re.I)
# A government matter that carries no case caption. "Ordinary course" is not a
# clean bill of health when one of these is also on the page: nearly every Item 1
# opens with "in the ordinary course of business, we are involved in various
# pending and threatened litigation matters", so the phrase alone cleared LUNR
# while a DOJ False Claims Act investigative demand sat 30k characters below it —
# a real matter, and one no caption test can see, since the government does not
# sue under a caption the filer prints.
_GOVT_MATTER_RE = re.compile(
    r"civil investigative demand|qui tam|False Claims Act"
    r"|formal investigation|grand jury|Wells notice|subpoena"
    r"|deferred prosecution|consent decree"
    r"|(investigation|inquiry) by the (SEC|DOJ|FTC|CFPB|Department|Securities)",
    re.I)

# A section this short carries no named matter — it is the boilerplate sentence.
_BOILERPLATE_CHARS = 700

# Structural and hypothetical uses of the same words. The charter's exclusive-
# forum clause names "any derivative action" without one existing; a cash-flow
# hedge note is full of "derivative counterparties"; Risk Factors warns about
# suits that have not been filed.
_HYPOTHETICAL_RE = re.compile(
    r"may\s+(be|become)\s+(the\s+)?(target|subject)"
    r"|have\s+been\s+subject\s+to\s+securities"
    r"|exclusive\s+forum|forum\s+for\s+.{0,40}derivative"
    r"|derivative\s+counterpart", re.I)


def _hit_in_live_context(rx, section: str, window: int = 500) -> bool:
    """True if ``rx`` matches somewhere that isn't hypothetical boilerplate."""
    return any(not _HYPOTHETICAL_RE.search(
                   section[max(0, m.start() - window):m.end() + window])
               for m in rx.finditer(section))


_MONEY = r"\$\s?\d[\d,]*(?:\.\d+)?\s*(?:million|billion)?"

# The amount has to be grammatically attached to the liability, not merely near
# it. A window-based match reads Apple's balance sheet as a legal accrual: a
# financial table puts "Accrued compensation" a few characters from a dozen
# figures, and "accrued" is also how every filer labels payroll. These patterns
# only fire on prose a filer writes about a case.
_EXPOSURE_RES = [re.compile(pat, re.I) for pat in (
    # "we accrued $150 million", "recorded a charge of $2 million"
    r"(?:accrued|accrual of|reserved|reserve of"
    r"|recorded\s+(?:a\s+)?(?:charge|liability|reserve|provision)\s+of)"
    r"\s+(?:approximately\s+|an\s+aggregate\s+of\s+)?" + _MONEY,
    # "judgment of $114 million", "reducing the damages amount to $114 million"
    r"(?:judgment|verdict|damages|penalt\w+|fines?|award|settlement)"
    r"\s+(?:amount\s+)?(?:of|to|totalling|totaling|in the amount of)"
    r"\s+(?:approximately\s+)?" + _MONEY,
    # "a $114 million judgment", "$25 million settlement"
    _MONEY + r"\s+(?:judgment|verdict|settlement|penalty|fine|award)",
    # "agreed to pay $30 million to settle"
    r"(?:agreed\s+to\s+pay|paid)\s+(?:approximately\s+)?" + _MONEY +
    r"\s+(?:to\s+settle|in\s+settlement|in\s+damages|in\s+penalties)",
    # "the jury returned a verdict … with a lump sum amount of $152 million"
    r"(?:jury|court|arbitrator)\s+[^.]{0,100}?"
    r"(?:awarded|returned\s+a\s+verdict|entered\s+judgment)[^.]{0,80}?" + _MONEY,
)]

# Filers accrue for plenty that is not a legal matter, in the same words:
# Teradyne's "product warranty accrual of $25.7 million" is not exposure.
_NON_LEGAL_ACCRUAL_RE = re.compile(
    r"warrant(y|ies)|vacation|payroll|compensation|bonus|restructuring"
    r"|rebate|sales? return|deferred revenue|income tax|interest expense"
    r"|dividend|lease|pension", re.I)
# And an amount can run the other way: BYND's $11.0 million settlement was
# "due and payable to us". A recovery is not an overhang.
_INBOUND_RE = re.compile(
    r"payable to us|to us within|we received|received \$|in our favou?r"
    r"|receivable|recovery of|awarded to us|paid to us|in favou?r of the Company",
    re.I)

def quantified_exposure(section: str) -> str | None:
    """The largest legal dollar figure stated in the note, or None.

    A booked or entered amount is the filer's own quantification of a matter —
    the one signal a case caption can never carry. PANW's three captions read
    like a docket of resolved nuisance suits; one of them is a $114 million
    judgment with $150 million accrued against it.
    """
    best, best_val = None, 0.0
    for rx in _EXPOSURE_RES:
        for m in rx.finditer(section):
            if _HYPOTHETICAL_RE.search(m.group(0)):
                continue
            before = section[max(0, m.start() - 90):m.start() + 40]
            if _NON_LEGAL_ACCRUAL_RE.search(before):
                continue
            around = section[max(0, m.start() - 150):m.end() + 200]
            if _INBOUND_RE.search(around):
                continue
            money = re.search(_MONEY, m.group(0), re.I)
            if not money:
                continue
            value = _money_value(money.group(0))
            if value > best_val:
                best, best_val = " ".join(money.group(0).split()), value
    return best


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


# A contents row lists several items in a row with their page numbers ("Item 1.
# Legal Proceedings 43 Item 1A. Risk Factors 43 Item 2. …"). The *page numbers*
# are the signature, not the item markers: counting bare "Item N" reads a real
# one-line Item 1 as a contents row, because a short section is immediately
# followed by the Item 1A / Item 2 / Item 3 headings that come after it. That
# misfire emptied AAP's corpus entirely and cut CLS, TXN and M down to their
# first sentence.
_CONTENTS_ROW_RE = re.compile(r"\d+\s+Item\s+\d", re.I)


def _is_contents_stub(section: str) -> bool:
    return len(_CONTENTS_ROW_RE.findall(section[:600])) >= 2


def extract_legal_section(txt: str, form: str) -> str:
    """The Legal Proceedings section, skipping the table-of-contents match.

    A 10-Q carries it at Part II Item 1, a 10-K at Item 3. Both names also occur
    in the contents listing near the top, so the *last* match that is followed by
    real prose is the section itself.

    Taking the *longest* match did the opposite of what it looks like: the
    contents row is the one with no end anchor near it, so it always ran the full
    8k fallback (or down to the real Item 1A tens of thousands of characters
    below) and always won. PANW's "legal section" was the contents page plus the
    balance sheet; WRBY's was 46% of the whole 10-Q.

    The end anchor accepts a bare ``RISK FACTORS`` heading as well as ``Item 1A``.
    Filers that write the heading without the item number left the search with
    nothing to stop at, so the 8k fallback ran on into the risk factors — which is
    how CBRS's "legal section" ended up carrying a risk-factor bullet about
    material weaknesses in internal control.
    """
    if form == "10-K":
        start_re, end_re = r"ITEM\s*3\.?\s*[—–-]?\s*LEGAL PROCEEDINGS", r"ITEM\s*4\.?"
    else:
        start_re = r"ITEM\s*1\.?\s*[—–-]?\s*LEGAL PROCEEDINGS"
        end_re   = r"ITEM\s*1A\.?|RISK FACTORS"
    best = ""
    for m in re.finditer(start_re, txt, re.I):
        tail = txt[m.start():m.start() + 60000]
        e = re.search(end_re, tail[40:], re.I)
        section = tail[:e.start() + 40] if e else tail[:8000]
        if _is_contents_stub(section):
            continue
        best = section
    return best.strip()


_NOTE_ANCHOR_RE = re.compile(
    r"Loss Contingencies|Legal Proceedings|Commitments and Contingencies"
    r"|Legal Matters", re.I)


# Risk Factors is hypothetical by construction — "we may be the target of
# securities class action litigation" is a warning, not a docket — so nothing in
# it is evidence of a live matter. Blanked before anchoring rather than filtered
# after, since the note windows would otherwise reach into it.
_RF_START = re.compile(r"ITEM\s*1A\.?\s*[—–-]?\s*RISK FACTORS", re.I)
_RF_END   = re.compile(r"ITEM\s*(1B|2)\.?", re.I)


def _mask_risk_factors(txt: str) -> str:
    """Blank the Risk Factors section — the last one that isn't a contents row.

    Same reasoning as extract_legal_section: an earlier "Item 1A" is a contents
    row or a cross-reference, and masking from there swallows the real legal
    note. Masking every match ate 140k characters of Honeywell's 10-Q, Flexjet
    v. Honeywell included. A missing end anchor means the span is unknown, so
    nothing is masked — losing a real matter is the more expensive mistake.
    """
    start = None
    for m in _RF_START.finditer(txt):
        if not _is_contents_stub(txt[m.start():m.start() + 400000]):
            start = m.start()
    if start is None:
        return txt
    e = _RF_END.search(txt[start + 40:start + 400000])
    if e is None:
        return txt
    span = e.start() + 40
    return txt[:start] + (" " * span) + txt[start + span:]


def extract_legal_corpus(txt: str, form: str) -> str:
    """Item 1/Item 3 plus the contingencies note windows, less Risk Factors.

    A 10-Q's Item 1 is often one line pointing at the notes ("see Note 12"), so
    classifying on Item 1 alone reads a live case as boilerplate. The note text
    is where the captions and the accrual actually are.
    """
    masked = _mask_risk_factors(txt)
    parts = [extract_legal_section(masked, form)]
    for m in _NOTE_ANCHOR_RE.finditer(masked):
        window = masked[m.start():m.start() + 4000]
        # The same contents-row guard extract_legal_section uses. An anchor can
        # land on the contents listing just as easily, and the window then runs
        # 4k characters through whatever front matter follows it — CRWV's
        # forward-looking-statements list, and its material-weakness bullet.
        if _is_contents_stub(window):
            continue
        parts.append(window)
    seen, out = set(), []
    for part in parts:
        key = part[:120]
        if part.strip() and key not in seen:
            seen.add(key)
            out.append(part)
    return "\n".join(out)


def classify(section: str) -> dict:
    """Propose a severity from one Legal Proceedings section, with evidence."""
    ev: list[str] = []
    if not section:
        return {"severity": None, "confidence": "low",
                "evidence": ["no section found"], "exposure": None,
                "exposure_only": False}

    caption     = _hit_in_live_context(_CAPTION_RE, section)
    heading     = bool(_NAMED_HEADING_RE.search(section))
    securities  = _hit_in_live_context(_SECURITIES_RE, section)
    derivative  = _hit_in_live_context(_DERIVATIVE_RE, section)
    credibility = _credibility_evidence(section)
    govt        = bool(_GOVT_MATTER_RE.search(section))
    unbounded   = bool(_UNBOUNDED_RE.search(section))
    exposure    = quantified_exposure(section)
    # Whether the amount is the only thing holding this entry up. A caption or a
    # class action stands on its own; a bare accrual has to clear a materiality
    # bar first, and only the consumer knows the market cap to measure it against.
    exposure_only = bool(exposure) and not (securities or derivative or caption)

    if caption:
        ev.append("named case caption")
    if heading:
        ev.append("broken-out legal heading")
    if securities:
        ev.append("securities class action (10b-5)")
    if derivative:
        ev.append("shareholder derivative suit")
    if credibility:
        ev.append(f"accounting integrity in question ({credibility})")
    if unbounded:
        ev.append("loss not estimable")
    if govt:
        ev.append("government investigation on file")
    if exposure:
        ev.append(f"quantified exposure ({exposure})")

    if securities and credibility:
        # The short-attack pattern: the risk is the accounting, not the case.
        # `credibility` is deliberately narrow — see _credibility_evidence.
        return {"severity": "existential", "confidence": "medium",
                "evidence": ev, "exposure": exposure, "exposure_only": False}

    # Presence of litigation is not the signal — every large filer has a docket,
    # and a standing "Antitrust Matters" or "Patent Litigation" heading is
    # baseline for mega-cap tech and pharma, not deviation from it. So a heading
    # on its own is not enough to size a position down: only a specific case
    # caption, a securities class action or a derivative suit acts. A heading
    # with nothing behind it goes to `review` for a human read.
    # A booked or entered dollar figure acts on its own: the filer has conceded
    # both that the matter is real and roughly what it costs.
    if securities or derivative or caption or exposure:
        return {"severity": "overhang",
                "confidence": "medium" if (securities or caption or exposure)
                              else "low",
                "evidence": ev, "exposure": exposure,
                "exposure_only": exposure_only}
    if heading:
        return {"severity": "review", "confidence": "low",
                "evidence": ev + ["heading only — no caption or case-type signal"],
                "exposure": exposure, "exposure_only": False}
    if not govt and (len(section) <= _BOILERPLATE_CHARS
                     or _ORDINARY_RE.search(section)):
        return {"severity": "clear", "confidence": "medium",
                "evidence": ev + ["ordinary-course language only"],
                "exposure": exposure, "exposure_only": False}
    # Substantial legal text with nothing decisive in it. Refusing to guess is
    # the point: defaulting to overhang here would cap the tier on most of the
    # S&P, and defaulting to clear would hide a real matter behind bad parsing.
    return {"severity": "review", "confidence": "low",
            "evidence": ev + ["legal text present but no decisive signal"],
            "exposure": exposure, "exposure_only": False}


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
    if verdict.get("exposure"):
        # Kept on the entry, not just in the note: it is the one field that says
        # how big the matter is, and the reason a caption alone never could.
        entry["exposure"] = verdict["exposure"]
    if verdict.get("exposure_only"):
        # The overhang rests on the amount alone, so it is only worth capping a
        # position for if the amount is material against the company's size —
        # a test `_risk_tier()` applies, since the market cap lives there.
        entry["exposure_only"] = True
    if verdict["severity"]:
        entry["severity"] = verdict["severity"]
        entry["note"] = "; ".join(verdict["evidence"])
    elif verdict["evidence"] == ["no section found"]:
        # Nothing could be extracted at all — that is a parsing failure, not a
        # clean filing. ADI and HD are large filers that certainly have legal
        # proceedings; recording them `clear` says the opposite of what happened.
        # `review` flags LITIGATION UNCLEAR without touching grade or sizing.
        entry["severity"] = "review"
        entry["note"] = ("no legal section could be extracted from this filing — "
                         "read Part II Item 1 by hand")
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
