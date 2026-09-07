# How a contract is graded

Every row in the LSO Analysis tab carries a letter grade. This is what produces
it, why each factor is there, and how much it moves the number.

The authority is `core/lso_analyzer.py`; this document describes it. If the two
disagree, the code is right and this file is stale.

## The shape of it

```
      70   base — every symbol starts here
   ±  ..   symbol factors    (analyze_symbol)        what the company is
   ±  ..   contract factors  (apply_contract_adjustments)  what this strike is
   ─────
   clamp to 0–100  →  A ≥ 85   B ≥ 70   C ≥ 55   D ≥ 40   F < 40
```

Two passes, because the two questions are different. *Is this a company I'm
willing to own?* is answered once per symbol from fundamentals. *Is this strike
worth selling?* is answered per contract and re-uses that symbol score as its
starting point — one company can produce an A at one strike and an F at another.

Every adjustment that fires also writes a line into **Notes**, and the severe
ones raise a short label in **Flags**. A grade is never a black box: the Notes
column is the full derivation, in the order applied.

## Gates and tilts

The magnitudes fall into two groups, and the split is deliberate.

**Gates** (−15 to −100) are disqualifiers — conditions under which the trade
shouldn't happen at any price. One gate is usually enough to sink a contract
from B to F on its own, which is the intent.

**Tilts** (±3 to ±5) rank the survivors. No single tilt changes a grade; two or
three agreeing ones will. They're for choosing between contracts that have
already cleared the gates.

If you find yourself wanting to overrule a gate, the gate is probably wrong for
your strategy — change the band rather than talking yourself past it.

## Symbol factors

Fundamentals from yfinance, evaluated once per symbol per scan.

### Sector

Wheel risk is mostly "will this gap through my strike overnight." Sectors are
scored by how prone they are to that.

| Sector | Adj | Why |
|---|---:|---|
| Utilities | +10 | Regulated demand; the least surprising price action there is |
| Consumer Defensive | +8 | People buy toothpaste in a recession |
| Real Estate | +5 | Income-oriented, generally stable |
| Industrials | +2 | Moderate; watch the macro cycle |
| Consumer Cyclical | 0 | Cycle exposure, but no structural gap risk |
| Communication Services | 0 | Mixed bag — some utilities-like, some not |
| Technology | 0 | The neutral reference point |
| Financial Services | −8 | Rate-sensitive; credit cycle exposure |
| Healthcare | −10 | Large pharma is fine; the sector average carries biotech |
| Basic Materials | −15 | Commodity price exposure, cyclical |
| Energy | −25 | Oil/gas swings plus geopolitical headline risk |

### Industry sub-type

A second pass for industries whose risk the sector average understates. First
keyword match wins, so a name picks up at most one.

| Industry contains | Adj | Why |
|---|---:|---|
| biotechnology | −20 | Trial and FDA results are binary and overnight |
| drug manufacturers | −15 | Pipeline news drives the price more than earnings |
| coal | −10 | Structural decline plus regulatory risk |
| uranium | −10 | Regulatory and geopolitical sensitivity |
| oil & gas | −5 | Direct crude exposure, on top of the Energy sector hit |

### Beta, market cap, dividend

| Factor | Band | Adj | Why |
|---|---|---:|---|
| Beta | < 0.50 | +8 | Moves less than the market; strike is less likely to be reached |
| | 0.50–0.80 | +5 | Below-market volatility |
| | ≥ 0.80 | 0 | Neutral — **high beta is annotated in Notes but never penalised** |
| Market cap | ≥ $10B | +5 | Liquid options, tight markets, survivable |
| | $2–10B | 0 | Neutral |
| | $0.5–2B | −10 | Option liquidity gets thin |
| | < $0.5B | −20 | Wide spreads; assignment in a name that can halve |
| Dividend | pays any | +5 | Pays you to hold the shares if assigned — the wheel's fallback plan |

Beta only ever adds. That asymmetry is intentional: σ-cushion already measures
volatility risk per contract, using the option market's own forward estimate,
which is better than beta at the thing beta would be used for. Beta's positive
band survives as a bonus for genuinely sleepy names.

### Earnings

| Condition | Adj | Why |
|---|---:|---|
| Earnings between now and expiration | **−40** | The single largest symbol-level gate. An earnings gap is exactly the move a short put can't absorb, and no premium compensates for it |
| Earnings within ~1 month after expiration | −5 | IV is elevated going in; you're selling into a rise that hasn't happened yet |

Earnings in the period also marks the symbol **gappy**, which raises the
σ-cushion requirement from 1.0σ to 1.5σ. It's the only factor that changes
another factor's threshold.

## Contract factors

Per strike, layered on the symbol score.

### OTM% — distance to the strike

| Band | Adj | Flag | Why |
|---|---:|---|---|
| In the money | **−100** | ITM STRIKE | Assignment isn't a risk, it's the current state |
| < 2% | −30 | NEAR ATM | No cushion; a normal day reaches it |
| 2–4% | −10 | MARGINAL OTM | Thin — only for low-beta, dividend-paying names |
| 4–8% | +3 | | Good cushion |
| 8–12% | +5 | | The sweet spot: real distance, premium still meaningful |
| 12–16% | −5 | | Wide; check the premium is still worth the capital |
| > 16% | −30 | WIDE OTM | A market warning label. If a strike this far out still pays 1%, the market is pricing severe downside — believe it |

The largest single term in the model, in both directions. Distance to the strike
is the primary thing a put seller controls.

### σ-cushion — distance in expected moves

OTM% divided by the underlying's expected move to expiration, from the option's
own implied volatility. The main volatility-adjusted risk measure, because 8% OTM
means something completely different on a utility than on a biotech.

The adequate mark is **1.0σ**, or **1.5σ for gappy names** — earnings in the
period, or the Energy / Basic Materials sectors. Bands step half a σ either side
of it; the gappy ladder is the whole thing shifted up 0.5σ.

| Band (normal / gappy) | Adj | Flag | Why |
|---|---:|---|---|
| no IV data | −5 | IV UNAVAILABLE | The gate couldn't be applied at all. Scoring an unknown at zero would let an unmeasured contract outrank a measured one, so it costs a little rather than nothing |
| < 0.5σ / < 1.0σ | −15 | SUB-0.5σ / SUB-1σ | Inside half an expected move — assignment takes *less* than a normal move |
| 0.5–1.0σ / 1.0–1.5σ | −5 | | Short of adequate; the premium has to earn its keep |
| 1.0–1.5σ / 1.5–2.0σ | 0 | | Adequate |
| ≥ 1.5σ / ≥ 2.0σ | +5 | | Strong protection |

This was a single −30 gate below the adequate mark. It was recalibrated because
the mark sat where most ordinary trades live: **57% of contracts passing the
premium filter came in under 1σ**, and a 0.5σ cushion is roughly a 0.28-delta
put — a mainstream wheel strike, not a reckless one. A gate that rejects the
majority of candidates isn't a gate, it's the sorting axis, and at −30 it
overrode everything else in the model. The graded version keeps a real penalty
for genuinely thin cushions and lets standard strikes compete on their premium
and technicals.

When a symbol's chain comes back without usable implied volatility — Schwab
occasionally answers with `-999` in every volatility field — this factor, IV%
and IV/HV all go blank together, since all three derive from it. The scan
retries such a symbol once, logs any that still fail, and the −5 above marks the
grade provisional.

Note this factor and OTM% measure the same distance, one raw and one
vol-adjusted, so a contract can be penalised by both. That's intended — they
disagree often enough to be worth reading separately — but it does mean the two
stack on the worst contracts.

### IV% — is the position manageable

| Band | Adj | Flag | Why |
|---|---:|---|---|
| < 25% | −15 | LOW IV | A grinder: thin premium to enter, thin to roll, thin covered calls if assigned. You can't manage your way out of a low-IV name |
| 25–80% | 0 | | Normal working range |
| > 80% | −8 | HIGH IV | Pays richly but gaps hard — size down |

This is the roll-ability factor. IV level, not IV rank, is what determines
whether a further-dated strike still holds enough time value to roll into.

### IV/HV — is the premium rich

Implied volatility over the underlying's own 20-day realized volatility.

| Band | Adj | Flag | Why |
|---|---:|---|---|
| ≥ 1.30 | +3 | | Options price more movement than the stock has been making — the gap a seller is paid for |
| 0.90–1.30 | 0 | | In line |
| < 0.90 | −3 | IV BELOW REALIZED | Selling the move cheaper than it's actually happening |

Replaced an IV-percentile signal that needed a year of accumulated scan history
before it meant anything. This works from the first scan and is comparable
across symbols.

### RSI and BB% — entry timing

Both from the stock scan, both measuring where price sits in its recent range.
They usually agree, so the combined swing is roughly ±10.

| RSI | Adj | Flag | | BB% | Adj | Flag |
|---|---:|---|---|---|---:|---|
| < 20 | −5 | RSI EXTREME | | < 0 | −5 | BELOW BAND |
| 20–40 | +5 | | | 0–33 | +5 | |
| 40–60 | 0 | | | 33–67 | 0 | |
| 60–70 | −3 | | | 67–100 | −3 | |
| ≥ 70 | −5 | OVERBOUGHT | | > 100 | −5 | ABOVE BAND |

A mild pullback is the entry the screen exists to find. An *extreme* reading is
penalised in both directions: capitulation and a break below the lower band mean
something changed, and assignment leaves you long a name in freefall. Overbought
is penalised because selling puts near a local high leaves nothing between you
and the mean.

### Spread% — can you get back out

Bid-ask spread as a percentage of the mid.

| Band | Adj | Flag | Why |
|---|---:|---|---|
| < 10% | +3 | | Tight; cheap to roll or close |
| 10–25% | 0 | | Workable for a weekly |
| 25–50% | −5 | WIDE SPREAD | A round trip gives back real premium |
| > 50% | −15 | NO MARKET | Getting out can cost more than the time value you sold |

Every roll is a buy-to-close plus a sell-to-open, so the spread is the toll on
managing the position — and it is worst on exactly the deep-ITM strikes where
rolling is the thing you need. Bands are set for weekly options on mid-caps,
which quote much wider than index options; 15–20% is normal here.

**Open interest** is displayed but not scored. Near zero means the quote is
theoretical whatever the spread says, but a thin-yet-tight market is still
tradeable, and the spread already captures most of it.

## Hard gates and risk tier

Two things sit outside the score, because the score is a single number and these
aren't matters of degree.

**Hard gates** disqualify outright: score 0, grade F, risk tier `Reject`, and the
flag `HARD REJECT`. Two things trigger one — a `biotech-binary` tag, or a listing
inside the IPO window. These are binary, un-priceable risks — no cushion or
position size compensates — so the rejection is sticky:
`apply_contract_adjustments()` returns early for a gated symbol, and a generous
chain can't re-score it back above F.

**Litigation is not one of them.** It flags and it caps the tier; it never
rejects. Two reasons, and the first is the stronger:

*A hard gate is self-defeating for a risk you intend to read case by case.*
`Reject` zeroes the score and short-circuits the contract pass, so a gated name
never reaches the table with a workable contract — the gate removes exactly the
candidate you would have wanted to look at. Flagging inverts that: the name
surfaces on its technicals, carries `LITIGATION SEVERE` / `LITIGATION OVERHANG` /
`LITIGATION UNCLEAR` in the `flags` column and the CSV export, and you read the
filing as a separate step before writing anything against it.

*And the severity is a machine's guess, not a fact.* The asymmetry that justifies
gating — a gate that wrongly stops firing risks real money, one that wrongly
keeps firing only costs a trade — holds for something you have checked. It does
not hold for a regex over a single filing. PYPL graded F on the phrase "Civil
Investigative Demand", which is routine language for any consumer-finance filer:
no restatement, no material weakness, no SEC or DOJ matter anywhere in the
document. Four of the five `existential` verdicts in the first automated pass
were extraction artifacts. A store that unverified can size a position down; it
should not be able to silently delete a candidate.

Litigation lives in its own store rather than in the durable tags — see
[Litigation is dated, not durable](#litigation-is-dated-not-durable) — and the
judgment behind an entry is in
[Deciding whether litigation counts](#deciding-whether-litigation-counts).
The strategy's other auto-disqualifiers — short-attack patterns, foreign
regulatory overhang, suspended guidance — still have nowhere to live and remain a
judgment call at review time.

**The IPO gate is derived, not tagged.** "Listed under 12 months" is a fact with
an expiry date, and a hand-written tag has no way to reach it. Six tickers once
carried a `recent-ipo` tag; by the time it was checked, CRCL, FIG and BLSH had
aged past the window and were still being force-rejected on it, silently. The
listing date rides along in the yfinance `info` the analyzer already fetches, so
`_ipo_gate_reason()` computes the age itself and the gate stops firing on its
own. The tag remains only as a fallback for when that field is missing.

**Risk tier** is a sizing output, not a quality one. The base tier follows the
graded score (Low ≥ 85, Medium ≥ 70, High below), since the score already weighs
the beta, market-cap and cushion inputs the sizing table describes. Two factors
then *cap* the tier at High without rejecting anything:

| Factor | Source | Effect |
|---|---|---|
| Crypto-linked | `crypto-linked` tag | Tier capped at High |
| Litigation overhang | `overhang` in `litigation.json` | Tier capped at High, unless it rests on an immaterial amount alone |
| Litigation, severe | `existential` in `litigation.json` | Tier capped at High, flag `LITIGATION SEVERE` |
| Unprofitable | TTM operating income, falling back to trailing EPS / net income | Tier capped at High |
| Funded by issuing paper | share count and total debt, quarterly balance sheet | `DILUTING` / `LEVERING` flag |
| IREN pattern | `core-revenue-declining` tag **and** funded-by-paper | Max allocation becomes "15% or pass" |

### Profitability uses operating income, not the bottom line

The bottom line lies. IREN's trailing EPS reads **+$0.77** on a one-off gain
booked below the operating line in a single quarter; its TTM operating income is
**−$221M**. Net income alone would have called it profitable and skipped every
caution the strategy asks for. Operating income leads, with trailing EPS and net
income as a backstop — either negative counts, because yfinance disagrees with
itself often enough (MSTR reports `profitMargins` 0.0 next to a −$31B net
income) that no single field can be trusted alone.

### Funded by issuing paper

Dilution and debt are alternative routes, not nested, so both are measured over
a one-year lookback on the quarterly balance sheet:

```
diluting = share count +20% YoY AND rising in ≥3 of the last 4 quarters
levering = net new debt ≥ 15% of market cap
funded_by_paper = unprofitable AND (diluting OR levering)
```

Three deliberate choices, each forced by a real name:

- **New debt is measured against market cap**, not against prior debt. RBRK's
  debt grew 243% off a tiny base — 5% of its market cap, i.e. nothing.
- **Dilution must be sustained.** Synopsys issued 23% of its shares in the
  single quarter to 2025-07-31 and was flat either side — the shape of a
  stock-funded acquisition, not an ATM programme. The three-of-four test
  excludes it; IREN rose in four of four.
- **OR, not AND.** IREN did both, MSTR only diluted, CLSK only borrowed.
  Requiring both would miss two of the three.

Gating on unprofitability keeps the flag off healthy companies in a capex build,
which is otherwise the same cash-flow shape.

Max allocation follows the tier: Low 35–40%, Medium 25–30%, High 15–20%.

A name can therefore grade B and still be capped at High — MSTR does exactly
that. That's the intent: profitability and crypto-linkage are sizing inputs, not
gates, so they shrink the position rather than exclude it.

**The remaining gap** is the declining core segment. Revenue by segment isn't in
any yfinance field, so `core-revenue-declining` stays hand-assigned in
`durable-tags.json`. Dilution is now measured, so a ticker carrying that one tag
completes the IREN pattern on its own. `active-dilution` also still works as a
manual tag, for overriding the measurement.

Both signals cost one extra yfinance fetch per symbol (quarterly income
statement and quarterly balance sheet), each guarded separately — a missing
statement degrades that one signal rather than failing the analysis.

**Tags** are mostly displayed rather than scored. Four act: `biotech-binary`
gates, `crypto-linked` caps the tier, and `core-revenue-declining` with
`active-dilution` set the IREN allocation. `recent-ipo` acts only as a fallback
when the listing date is missing. Every other tag is read by you, not by the
score. They come from `durable-tags.json`, a
hand-researched map of ticker → risk tags (`ai-capex-chips`,
`commodity-geopolitical`, `rate-sensitive-growth`, …) naming what actually moves
a name when the sector label doesn't. Nothing in the grade uses them: the grade
judges one contract in isolation, while the rest are for reading *across* the
table — four A-grade contracts sharing `ai-capex-chips` are one bet on one
factor, which no per-contract score can see. A ticker mapped to `[]` has been
researched and carries no durable tag; a ticker absent from the file shows
**UNTAGGED** and is listed in the status line and log so it can be added.

### Litigation is dated, not durable

Litigation does not live in `durable-tags.json`, and the distinction is the whole
point. A durable tag describes what a company *is* — a China ADR, a crypto proxy,
an AI-capex name — and that holds for years. A docket is a *dated fact*: cases are
filed, dismissed, settled and appealed, and an assessment written today is stale
within a quarter or two. `durable-tags.json` has nowhere to record when a fact was
established, so a litigation entry kept there would rot in place while still
gating a symbol. That is not hypothetical — it is exactly what happened to
`recent-ipo`, the one other time-boxed fact that was stored as a tag.

So litigation gets its own store, `litigation.json`, keyed by ticker:

```json
{
  "APP": {
    "severity":  "overhang",
    "asof":      "2026-08-29",
    "review_by": "2026-11-15",
    "case":      "Brownback v. AppLovin, N.D. Cal.",
    "exposure":  "$114 million",
    "note":      "10b-5 class action; MTD fully briefed Feb 2026, ruling pending",
    "source":    "https://www.sec.gov/Archives/edgar/data/1751008/.../app-20260630.htm"
  }
}
```

`exposure` is the largest legal dollar figure the filer stated — an accrual, an
entered judgment, a penalty — and is present only when the filing quantified one.
It informs sizing and never touches the score. A companion flag `exposure_only`
marks an entry whose overhang rests on the amount and nothing else — no caption,
no class action, no derivative suit — which is the only case the materiality bar
below applies to.

`severity` is one of four. None of them reject; the two below act on sizing:

| Severity | Effect |
|---|---|
| `existential` | Tier capped at High, flag `LITIGATION SEVERE` — read the filing before writing against it |
| `overhang` | Tier capped at High (an `exposure_only` entry must first clear the materiality bar) |
| `review` | No effect on grade or sizing; flags `LITIGATION UNCLEAR` |
| `clear` | No effect — records that the filing was checked and nothing was broken out |

`review` and `clear` exist so the automated pass can record what it found without
inventing a verdict.
`asof` and `review_by` carry the shelf life: `review_by` is explicit when you set
it, otherwise `asof` plus 100 days, which lands on the next quarterly report —
the filing that actually moves these facts. `case`, `note` and `source` are
provenance, so a future reader can check the finding instead of trusting it.

**A stale entry keeps applying, and says so.** Past `review_by`, the verdict does
not change; the symbol gains a `LITIGATION REVIEW DUE` flag and a note naming the
date. The two failure modes are not symmetric — a gate that wrongly stops firing
can put you short puts into a live disaster, while one that wrongly keeps firing
only costs you a trade — so staleness is surfaced, never silently resolved in
either direction. An entry with no usable date is stale from the start: undated
is unverified. An unreadable `severity` degrades to `overhang` and is flagged,
because a malformed entry is a data error, not a clean bill of health.

Because the entries are local data rather than market data, they survive a
yfinance failure: the flags and the tier cap still apply on the error path even
when a symbol's `analyze_symbol()` call gets no market data at all.

#### The review runs itself, for candidates only

`core/litigation_review.py` refreshes the store at the top of `analyze_symbols()`
— so LSO Analysis checks the filings for exactly the symbols it is grading. It is
deliberately **not** a universe sweep. Litigation status is only actionable for a
name you might write against, and it decays; pre-computing it across thousands of
symbols would mean maintaining a large file of judgments that are stale by the
time any of them matters. If a symbol becomes a candidate later, it gets read
then, and the answer is current.

`litigation.json` is what makes that cheap: only entries that are missing or past
`review_by` are fetched, so a repeat analysis costs nothing and the quarterly
refresh happens on its own. Roughly two EDGAR requests per symbol per quarter,
paced under SEC's 10 req/s limit.

Per symbol it reads the newest 10-Q or 10-K, extracts Part II Item 1 / Item 3
**plus the contingencies-note windows** — a 10-Q's Item 1 is usually one line
pointing at the notes, so classifying on Item 1 alone reads a live case as
boilerplate — **less the Risk Factors section**, which is hypothetical by
construction ("we *may* be the target of securities class action litigation")
and so is never evidence of a live matter. It then matches the rubric's
mechanical tests:

| Signal | Verdict |
|---|---|
| Securities class action **and** an accounting-integrity signal | `existential` |
| Securities class action, derivative suit, named case caption, or a quantified exposure | `overhang` |
| A broken-out heading only (`Antitrust Matters`, `Patent Litigation`) | `review` |
| A government investigation with no case caption | `review` |
| Substantial legal text, nothing decisive | `review` |
| Nothing could be extracted from the filing | `review` |
| Ordinary-course language only | `clear` |

**The credibility overlay reads the subject, not the regulator.** The
`existential` rule pairs a securities class action with evidence that the
*accounting* is in question — the short-attack pattern, where the risk is that
every other input on this page is wrong too. Distinguishing that from ordinary
regulatory conduct takes two tiers:

*Tier 1 — the books themselves*, which stands alone: a restatement, non-reliance,
an identified material weakness, an auditor resignation, accounting
irregularities, an audit-committee investigation, a Wells notice, a formal order
of investigation. Nobody writes these about a commercial dispute.

*Tier 2 — an investigative demand*, which counts only if a disclosure regulator
is named, that regulator is the **nearest** named authority to the demand (filers
group their regulatory matters into one paragraph, so an SEC mention three
matters away must not launder an FTC demand), **and** the accounting is what is
being asked about.

All three conditions, because any two of them are met by an ordinary FCPA or
False Claims Act matter. The authority alone is worthless as a test: on the ten
filings that tripped the old overlay, every demand-based hit was the SEC or the
DOJ asking about something other than the numbers — FCPA exposure (BSX), routine
healthcare investigations (CVS), a False Claims Act cybersecurity case (LUNR),
the HB6 bribery scandal (VST), anti-money-laundering in money transfer (WMT).
All real; none a reason to distrust a balance sheet. The old rule matched a bare
`subpoena` or `civil investigative demand` anywhere in the corpus, which is how
PYPL graded F on an FTC question about merchant onboarding.

Across the whole 162-symbol store the narrowed overlay fires three times, and the
one it is built for reads exactly right — Honeywell: *"The Company is cooperating
with a formal investigation by the SEC which is focused on certain financial
reporting matters."*

**"Ordinary course" is not a clean bill of health on its own.** Nearly every
Item 1 opens with *"in the ordinary course of business, we are involved in
various pending and threatened litigation matters"*, so a bare phrase match
clears a filing that also contains a real matter 30k characters below it. LUNR
read `clear` while carrying a Department of Justice civil investigative demand
over alleged False Claims Act violations — a matter no caption test can see,
because the government does not sue under a caption the filer prints. So a
government-matter signal (investigative demand, qui tam, False Claims Act, grand
jury, formal investigation, subpoena, consent decree, a named-agency inquiry)
blocks `clear` and lands on `review`. It is deliberately not `overhang`: an
investigation is not a verdict, and on the 162-symbol store only two filings
carry one without a caption already outranking it.

**A verdict needs text to read.** An empty extraction is recorded as `review`,
not `clear`. ADI and HD are large filers that certainly have legal proceedings;
their 10-Qs simply defeat the section parser, and writing "checked, nothing
broken out" for them says the opposite of what happened.

**A quantified exposure outranks a caption.** A case caption says a matter
exists; a booked accrual or an entered judgment is the filer's own statement of
what it costs, and it is the only signal that separates a live matter from a
docket of resolved nuisance suits. PANW's three patent captions read identically
whether the cases are dead or not — and one of them carries a $152 million jury
verdict, reduced to a $114 million judgment, with $150 million accrued against
it. The amount has to be grammatically attached to the liability rather than
merely near it, or a balance sheet reads as an accrual: "Accrued compensation"
sits a few characters from a dozen figures in every financial table. Amounts
running the other way (a settlement "due and payable to us") and non-legal
accruals (product warranty, payroll, restructuring) are excluded.

**But an amount alone has to be material.** A quantified exposure that is the
*only* evidence is marked `exposure_only`, and caps sizing only if it clears
**0.25% of market cap**. A filer booking a number has said the matter is real; it
has not said the matter is large. AXP's $12.5 million accrual is 0.006% of the
company — capping American Express from Low to High over it halves the position
for nothing.

An absolute floor gets this backwards, because the dollar figure and the
materiality run in different directions. ALGN's $31.8 million is a quarter the
size of RCL's $130 million and more material than it: 0.28% of an $11 billion
company against 0.18% of a $71 billion one. Only the ratio separates them.

| | exposure | market cap | % of cap | caps sizing |
|---|---:|---:|---:|:--|
| AXP | $12.5M | $220B | 0.006% | no |
| LOW | $12.5M | $115B | 0.011% | no |
| BSX | $42M | $69B | 0.061% | no |
| GLW | $85M | $133B | 0.064% | no |
| CDNS | $128.5M | $81B | 0.159% | no |
| RCL | $130M | $71B | 0.183% | no |
| ALGN | $31.8M | $11B | 0.282% | **yes** |
| CCL | $110M | $32B | 0.342% | **yes** |
| CVS | $542M | $124B | 0.438% | **yes** |
| BA | $971M | $168B | 0.579% | **yes** |

The test runs in `_risk_tier()` rather than in the review pass, because that is
where the market cap has already been fetched — `litigation_review` stays
pure-EDGAR and takes on no yfinance dependency for it. An unknown cap (the
yfinance error path) or an unparseable figure means the test cannot run, and an
untested overhang keeps applying: the same asymmetry the rest of the store is
built on, where a cap that wrongly fires costs a trade and one that wrongly
stands down costs a position. The stored entry is never rewritten — the severity
stays `overhang` and the reason lands in the notes, so the amount stays in front
of you either way.

A standing antitrust or patent heading is baseline for mega-cap tech and pharma,
not deviation from it, so it is explicitly **not** enough to size a position
down — it goes to `review` for a human read. On a twelve-name sample the pass
acted on six and handed five back; AAPL, MSFT, MRK and GOOGL all landed in
`review` on heading-only evidence, which is the correct answer.

**Two more extraction traps, found by re-reading what the classifier saw.** A
10-Q whose next heading reads `RISK FACTORS` without an item number left the end
anchor with nothing to match, so the 8k fallback ran on into the risk factors —
which is how CBRS's "legal section" acquired a risk-factor bullet about material
weaknesses in internal control. And the contents-row guard belongs on the note
windows too, not just on Item 1: an anchor lands on the contents listing just as
easily, and CRWV's window ran 4k characters through the forward-looking-statements
list, picking up its material-weakness bullet and pairing it with a real
securities class action to produce a spurious `existential`.

The guard itself had to change with them. It counted bare `Item N` markers, which
works only while sections are bloated — once they are correctly short, a genuine
one-line Item 1 is immediately followed by the Item 1A / Item 2 / Item 3 headings
and reads as a contents row. That emptied AAP's corpus outright and cut CLS, TXN
and M down to their first sentence. The *page numbers* are the real signature of
a contents listing (`43 Item 1A`), not the item markers.

**What the corpus excludes, and why.** Every mechanical test above is only as
good as the text it reads. Two extraction bugs made it read most of the filing:
`extract_legal_section()` kept the *longest* Item 1 match, which is always the
table-of-contents row (that one has no end anchor near it, so it ran to the real
Item 1A tens of thousands of characters below), and the note windows reached
into Risk Factors. The corpus averaged a quarter of the whole document. That
produced flags on cited case law — *South Dakota v. Wayfair* in a tax note read
as a caption for DDOG, *Loper Bright v. Raimondo* for KO — and, in the more
expensive direction, buried real matters: SNPS and AXTI both read `clear` while
carrying live securities class actions. Related: a charter's exclusive-forum
clause names "any derivative action" without one existing, and a putative class
action is only a *securities* class action when the filing says so — LUV's is a
wage-and-hour case, and reading it as securities alongside the credibility
overlay hard-gated the symbol to grade F.

**Confirm-to-clear.** Machine entries are marked `auto: true` and carry their
`evidence`, `confidence` and the filing they came from. Setting `confirmed: true`
on an entry protects it: a later automated pass may still *raise* the severity —
that direction is safe — but can never downgrade or clear a human verdict. What
it found is recorded alongside as `auto_severity` instead, so a parsing miss
cannot quietly un-gate a name you rejected on purpose.

### Deciding whether litigation counts

Litigation is not a binary the way an FDA readout is, and *presence* of it is not
a signal at all. Large pharma, autos, banks and insurers carry hundreds of open
matters at any time; a screener that tagged every company with an active lawsuit
would tag half the market and tell you nothing. The tag is for **deviation from
that industry's own baseline**, not for the existence of a docket.

Four questions decide it, in order. A headline is not enough to answer any of
them — all four are answered from the company's filings. The automated pass
answers questions 1 and, partly, 2; questions 3 and 4 are why `review` exists
and why a severity is worth confirming by hand.

**1. Does the company itself name it?** 10-K Item 3 *Legal Proceedings*, 10-Q Part
II Item 1, and the contingencies footnote. Routine matters get one boilerplate
paragraph: ordinary course, no material adverse effect expected. A matter that
gets its own named subsection with a case caption is management telling you this
one is different. This is the cheapest filter and it does most of the work — if a
company with a big legal footprint hasn't broken a case out, it's baseline.

**2. Is there a number, and how big is it against market cap?** The contingencies
footnote carries the accrued reserve and, often, a "reasonably possible loss in
excess of amounts accrued" range. Take the top of that range over market cap:

| Exposure | Read |
|---|---|
| Under ~2%, fully accrued | Baseline — no tag |
| ~2–10%, or accrued with a wide range | `litigation-overhang` |
| Over ~10–15%, or exceeds cash plus a year of operating income | `litigation-existential` |

Treat "cannot reasonably be estimated" on a case management *has* broken out as
a red flag, not as a neutral. It means the tail isn't bounded, which is exactly
the condition a cushion can't price.

**3. Does it threaten the revenue line, or only cost money?** This is the question
that separates the two tags most reliably, and it can override the arithmetic
above. An injunction, a forced divestiture, a licence or approval at risk, or a
remedy that rewrites how the product reaches customers is *structural* — tag it
existential even when the damages look affordable, because the loss isn't the
payment. A pure damages claim is a cash cost against a known balance sheet:
overhang.

**4. Is there a scheduled binary date, and does it land inside your expiration?**
A trial date, a verdict, an appellate ruling, a regulator's decision deadline. An
overnight gap on a ruling is the risk the wheel structurally cannot cushion —
the same reason earnings inside the contract's life is flagged. A date inside the
expiration is the reason to skip that expiration by hand — the severities
themselves no longer reject, so a dated binary is something you act on at review
time rather than something the store does for you.

**The credibility overlay.** A securities class action filed after a sharp drop,
alongside an SEC or DOJ investigation, a restatement, or an auditor change, is
not really a litigation question — it's the short-attack pattern the strategy
auto-disqualifies at review time. Mark it `existential` regardless of the dollar
figures. The risk there is the accounting, which means every other input on this
page is suspect too — which is also why the flag is worth reading rather than
delegating: an *investigation of the books* is the pattern, and a Civil
Investigative Demand from the FTC or CFPB about ordinary conduct is not it.

**What stays untagged.** Ordinary product liability for an automaker or a drug
maker. Patent disputes between operating companies over non-core products. NPE
suits. Routine wage-hour and employment class actions. Ordinary-course regulatory
examinations at a bank or insurer. And settled mass torts already on a fixed
payment schedule — the number is known and financed, which makes it an earnings
drag that the profitability and cash-flow factors already see.

**Sources**, all free and all primary:

- **SEC EDGAR full-text search** (`efts.sec.gov/LATEST/search-index?q=`) — then
  read the filing: 10-K Item 3, 10-Q Part II Item 1, 8-K Items 8.01 and 7.01.
- **The contingencies footnote** in the 10-K/10-Q — where the accrual and the
  loss range actually live. Item 3 often just cross-references it.
- **Stanford Securities Class Action Clearinghouse** — securities suits by
  ticker, with docket status.

**Re-check on each 10-Q**, and treat an 8-K on a docket event as a trigger to
re-read. That cadence is what `review_by` encodes, and why an entry past it is
flagged rather than trusted. Litigation resolves: a settled case should be removed
from `litigation.json`, not left to gate a symbol forever.

**An entry is not a fetched fact.** None of this is in the screener — no field it
requests mentions a lawsuit, and nothing verifies these entries. A ticker absent
from `litigation.json` means *not researched*, not *no litigation*.

## Where the numbers come from

| Input | Source |
|---|---|
| Sector, industry, beta, market cap, dividend, earnings date | yfinance (`Ticker.info`, `Ticker.calendar`) |
| Tags | `durable-tags.json`, hand-maintained (re-read when the file changes) |
| Strike, premium, bid/ask, delta, IV, open interest | Schwab option chain, live |
| RSI, BB%, realized volatility | The stock scan's own daily closes, via the history store |
| OTM%, σ-cushion, IV/HV, spread% | Computed in `core/screener.py` from the above |

## Reading a grade

An F is not "a bad company." It nearly always means one gate fired, and the
Notes column names it. A −40 for earnings in the period will sink an otherwise
excellent symbol, and correctly so — the fix is a different expiration, not a
different stock.

Conversely, an A means nothing fired and several tilts agreed. It is not a
prediction; it's the absence of known objections.
