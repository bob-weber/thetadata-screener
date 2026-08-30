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
flag `HARD REJECT`. Three things trigger one — a `biotech-binary` tag, a listing
inside the IPO window, or an `existential` entry in `litigation.json`. These are
binary, un-priceable risks — no cushion or position size compensates — so the
rejection is sticky: `apply_contract_adjustments()` returns early for a gated
symbol, and a generous chain can't re-score it back above F. They're driven off
`durable-tags.json` rather than sector, because sector doesn't identify them.

Securities litigation is one of the strategy's auto-disqualifiers, and it lives
in its own store rather than in the durable tags — see
[Litigation is dated, not durable](#litigation-is-dated-not-durable). An
`existential` entry gates; an `overhang` entry caps the tier. Neither is
*derived* — no field the screener fetches mentions a lawsuit — so both are
hand-assigned, and the judgment behind them is in
[Deciding whether litigation counts](#deciding-whether-litigation-counts).
The remaining auto-disqualifiers — short-attack patterns, foreign regulatory
overhang, suspended guidance — still have nowhere to live and remain a judgment
call at review time.

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
| Litigation overhang | `overhang` in `litigation.json` | Tier capped at High |
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
    "note":      "10b-5 class action; MTD fully briefed Feb 2026, ruling pending",
    "source":    "https://www.sec.gov/Archives/edgar/data/1751008/.../app-20260630.htm"
  }
}
```

`severity` is one of four, and only two of them act:

| Severity | Effect |
|---|---|
| `existential` | Hard gate — score 0, grade F, tier `Reject` |
| `overhang` | Tier capped at High |
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
yfinance failure: a symbol on file as existentially sued stays rejected even when
its `analyze_symbol()` call errors out.

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
boilerplate — and matches the rubric's mechanical tests:

| Signal | Verdict |
|---|---|
| Securities class action **and** regulator/restatement language | `existential` |
| Securities class action, derivative suit, or a named case caption | `overhang` |
| A broken-out heading only (`Antitrust Matters`, `Patent Litigation`) | `review` |
| Substantial legal text, nothing decisive | `review` |
| Ordinary-course language only | `clear` |

A standing antitrust or patent heading is baseline for mega-cap tech and pharma,
not deviation from it, so it is explicitly **not** enough to size a position
down — it goes to `review` for a human read. On a twelve-name sample the pass
acted on six and handed five back; AAPL, MSFT, MRK and GOOGL all landed in
`review` on heading-only evidence, which is the correct answer.

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
expiration promotes `litigation-overhang` to `litigation-existential` for that
expiration, even if it would otherwise only cap the tier.

**The credibility overlay.** A securities class action filed after a sharp drop,
alongside an SEC or DOJ investigation, a restatement, or an auditor change, is
not really a litigation question — it's the short-attack pattern the strategy
already auto-disqualifies. Tag it existential regardless of the dollar figures.
The risk there is the accounting, which means every other input on this page is
suspect too.

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
