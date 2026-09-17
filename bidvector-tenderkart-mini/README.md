# BidVector — a mini TenderKart with a bid-pricing engine on top

Tender discovery for Indian public procurement, plus the thing no tender
platform currently does: it tells you **what to bid**.

TenderKart-class products answer *"which tenders exist, and did I qualify?"*
That is a search and parsing problem, and it is solved. BidVector answers
*"should I bid, at what price, and what is my expected profit?"* — which is a
**sealed-bid first-price auction problem**, and treats it as one.

```
python3 run.py
```

Then open <http://127.0.0.1:8000> and sign in with **demo@bidvector.in /
demo1234**.

No `pip install`. No `npm install`. No API keys, no network access, no build
step. Python 3.9 or newer and nothing else — every dependency is in the
standard library, and the charts, the auth, the HTTP routing and the statistics
are all written out in this repo where you can read them.

---

## What is in it

**Discovery and screening** — search 200 live tenders by keyword, buyer,
category, state and closing window; the eligibility of every result is checked
against your company profile as you scroll, so you never open one you were
never qualified for. CSV export.

**Eligibility parsing** — a rule-based parser reads the free-text clause block
that every tender document carries and pulls out the turnover threshold,
experience requirement, similar-work value, required registrations and the
Make-in-India local-content class. It is regex over the phrasing that actually
recurs in CPPP and GeM documents, not an LLM call: it runs offline, it costs
nothing, and when it cannot read a clause it says so instead of inventing a
number. Paste any tender's clauses into the Clause parser tool to watch it work.

**The pricing engine** — the reason this exists. Details below.

**EMD allocator** — every bid locks Earnest Money Deposit until the tender is
decided, so working capital, not enthusiasm, is what limits how many tenders an
MSME can chase. Choosing this month's bids is therefore a two-constraint 0/1
knapsack, solved exactly by dynamic programming, and the app reports the
**shadow price of capital**: what the next ₹1 lakh of working capital would add
in expected profit, which is the number that tells you whether an overdraft
facility pays for itself.

**Bid pipeline** — watching → preparing → submitted → won/lost, with EMD
committed, cost estimate and realised margin per bid.

**Competitor intelligence** — who bids in each bucket, how often they win, their
average bid ratio and their deepest observed cut.

**Collusion screens** — the structural screens competition authorities run over
procurement data, ranked so you know which buckets to avoid or to report.

**Command centre** — capital locked in EMD, expected value of live bids, hit
rate, and every open tender ranked by expected profit rather than by deadline.

---

## The pricing engine

A government tender is a sealed-bid first-price procurement auction: everyone
submits once, the lowest compliant bid (L1) wins, nobody sees the others until
the bids are opened. Here is the whole chain, and all of it is in
`backend/engine/pricing.py`.

**1. Normalise.** Bids are only comparable across tenders once you divide by the
published estimate, so everything works in *bid ratios* `r = bid / estimated_value`.
A ₹3 Cr pipeline job and an ₹80 L pipeline job then live on the same axis.

**2. Estimate the rival distribution.** Bucket the historical bids by
`buyer × category × value band` and take the empirical CDF `G` of rival bid
ratios. Where the exact bucket is thin the model falls back down a hierarchy —
buyer×category, then category×band, then category, then everything.

**3. Shrink, because government procurement data is thin almost everywhere.**
A bucket with four past tenders is close to noise, so its ECDF is blended with a
wider category-level prior:

```
F(x) = w · F_bucket(x) + (1 − w) · F_prior(x),      w = n / (n + k),  k = 8
```

A bucket needs 8 observations before its own evidence outweighs the prior. This
one line does more for real-world accuracy than any fancier model would, and the
UI shows you `w` on every recommendation.

**4. Win probability.** If we bid ratio `b` against `N` rivals drawing
independently from `G`, we win when all of them bid above us:

```
P(win | b, N) = (1 − G(b))^N
```

`N` is not known either, so this is averaged over the empirical distribution of
bidder counts in the bucket.

**5. Expected profit.** With cost ratio `c = your cost / estimated value`:

```
π(b) = (b − c) · V · P(win | b)
```

Unimodal in the ordinary case — bid high and the margin is good but you never
win, bid low and you win work that is not worth doing. Evaluate on a grid, take
the argmax, and that is a rupee figure you can type into the BoQ.

**6. Bootstrap everything.** The historical bids and the bidder counts are
resampled 160 times and the whole solve is repeated, so the output is an
interval on the optimal bid, not a fake-precise point. A recommendation built on
4 tenders and one built on 60 are not the same object and the interface refuses
to present them as though they were: every screen carries the bucket size, the
shrinkage weight and a plain-language confidence note, and at very low `n` it
tells you outright not to bid off the number.

**7. Structural extension (Guerre–Perrigne–Vuong).** The bidder's first-order
condition inverts:

```
c = b − (1 − G(b)) / ((n − 1) · g(b))
```

so with a nonparametric `G` (empirical CDF) and `g` (Gaussian KDE) you can
recover rivals' unobserved *costs* from their observed bids. The app reports the
median recovered cost and the implied markup, which answers a question a search
tool cannot: is there any room in this market at all, or is everyone already
bidding at cost?

### What this does not do

It does not know your costs. Cost is the one input the model cannot infer, the
app defaults to a placeholder of 82% of the estimate, and every number moves
with whatever you replace it with. It also assumes rivals bid independently from
a stable distribution — which is exactly what the collusion screens exist to
test, and the bucket with the highest screen flags in the demo data is the one
where you should trust the pricing least.

---

## The collusion screens

Standard structural screens from the OECD bid-rigging guidelines and the
screening literature. None of them prove anything; they rank buckets by how far
their bid patterns sit from what independent competitive bidding produces, so a
human knows where to look.

| Screen | What fires it |
|---|---|
| Within-tender dispersion | Independent bidders costing the same job disagree. Persistent sub-3% coefficient of variation does not happen by itself. |
| L1–L2 spread vs loser clustering | A comfortable winner with a tight, uninterested pack above is the cover-bidding signature. |
| Win concentration | HHI over winners. Weak alone — one firm may simply be better. |
| Bid rotation | Each firm's losing bids sitting far above its own winning bids. A firm's price should not know in advance whether it is going to win. |
| Repeated pairing | Firms entering the same tenders more than their individual participation rates predict. |
| Round-number clustering | BoQ-derived bids land on arbitrary digits. Negotiated ones land on round figures. |
| Benford first digit | Reported because auditors ask, and **excluded from the risk score** — bids anchored to a published estimate are not a naturally Benford population, so a departure here means little. |

The screens are calibrated to avoid crying wolf: the pairing screen requires a
real co-occurrence count *and* a non-trivial baseline before a lift ratio is
allowed to say anything, because 3 meetings against 0.4 expected is an 8×
"lift" that means nothing. In the demo corpus one bucket is seeded with a
genuine cover-bidding ring and it ranks first; the clean buckets come back
normal.

---

## Layout

```
run.py                     entry point: seeds if needed, then serves
backend/
  server.py                ~150-line HTTP framework on http.server
  api.py                   every REST route
  db.py                    SQLite schema and access
  auth.py                  PBKDF2 hashing, HMAC-signed tokens
  seed.py                  synthetic corpus generator
  engine/
    stats.py               ECDF with shrinkage, KDE, bootstrap, quantiles
    pricing.py             the auction model
    eligibility.py         clause parser and profile matcher
    portfolio.py           EMD knapsack + shadow price
    cartel.py              the screens
frontend/
  index.html               the whole shell
  app.js                   hash-routed SPA, no framework
  charts.js                SVG charts, no charting library
  styles.css               light and dark, one stylesheet
tests/run_tests.py         49 tests, stdlib unittest
data/tenderkart.db         created on first run
```

## Running it

```bash
python3 run.py                  # seed if needed, serve on :8000
python3 run.py --port 9000      # different port
python3 run.py --open           # and open a browser
python3 run.py --reseed         # wipe and regenerate the demo corpus
python3 tests/run_tests.py      # 49 tests
```

## Tests

49 tests covering the places where being wrong would be invisible: that the
ECDF is a genuine CDF and the shrinkage weight follows `n/(n+k)`, that the KDE
integrates to 1, that win probability is monotone and equals `survival^N`
exactly, that the profit optimum lies above cost and rises with cost, that the
GPV inversion returns a cost below the observed bid, that the knapsack never
breaches the capital or bid-count constraint and never loses to the greedy
baseline, and that the clause parser reads crore/lakh/Indian-comma amounts
correctly and flags what it cannot read instead of guessing.

## The demo data

Generated, not scraped — 900 historical awards with full bid tables three years
deep, 5,000+ recorded bids, 200 live tenders across 14 real procurement bodies
(PHED Rajasthan, MSEDCL, PMC, CPWD, NHAI, BSNL, South Western Railway, BMC,
KSRTC, TANGEDCO and others). Bid ratios are drawn per bucket with their own mean
and dispersion so buckets genuinely differ; thin buckets are left thin so the
shrinkage and the low-confidence warnings are exercised rather than hidden; one
bucket carries a seeded cover-bidding pattern so the screens have a true
positive to find. The RNG is fixed, so two people running this see the same
numbers.

Swapping in real data means replacing `backend/seed.py` with an ingest against
the CPPP/GeM feeds. Everything downstream reads from the `tenders`, `awards` and
`bids` tables and does not care where the rows came from. **One thing to verify
before building on this for real:** the whole pricing approach depends on
per-bidder historical bid amounts being available in bulk. If a source only
returns single-tender detail on demand, the bucketing strategy needs rethinking
— and you want to know that on day one.

## Notes on the implementation

Written against the standard library on purpose. The environment this was built
in had no package registry access, and the constraint turned out to be worth
keeping: it costs about 150 lines of HTTP framework and buys a project that runs
on a fresh machine, offline, with one command and no version conflicts. The
trade is real, though — in production you would want a proper ASGI server, a
real JWT library, and `scipy.stats` instead of hand-rolled kernel density
estimation.

`BIDVECTOR_SECRET` is read from the environment for token signing and falls back
to a development constant. Set it before putting this anywhere real, and put it
behind HTTPS.
