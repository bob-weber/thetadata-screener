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

## Where the numbers come from

| Input | Source |
|---|---|
| Sector, industry, beta, market cap, dividend, earnings date | yfinance (`Ticker.info`, `Ticker.calendar`) |
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
