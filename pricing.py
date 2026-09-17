"""
BidVector pricing engine -- the part that makes this more than a search box.

A government tender is a sealed-bid first-price procurement auction: everyone
submits once, the lowest compliant bid (L1) wins, and nobody sees the others
until the technical bids open. That is a solved problem in auction theory, so
we solve it instead of guessing.

The chain of reasoning:

1.  Rivals' bids are only comparable across tenders once you normalise them.
    We work with the *bid ratio* r = bid / estimated_value, so a Rs 3 Cr
    pipeline job and a Rs 80 L pipeline job live on the same axis.

2.  Estimate G, the distribution of one rival's bid ratio, from the historical
    bids in the closest matching bucket (buyer x category x value band),
    shrunk toward a wider prior because most buckets are thin.

3.  If we bid ratio b against N rivals drawing independently from G, we win
    when every one of them bids above us:

        P(win | b, N) = (1 - G(b))^N

    N is not known either, so we average over the empirical distribution of
    bidder counts in that bucket.

4.  Expected profit as a function of our bid, with cost ratio c = cost / value:

        pi(b) = (b - c) * V * P(win | b)

    That function is unimodal in the usual case: bid high and you almost never
    win, bid low and you win worthless work. We evaluate it on a grid and take
    the argmax. That number -- a rupee figure you can type into the BoQ -- is
    the output no tender-search product gives you.

5.  Everything is bootstrapped. A recommendation from 4 past tenders and one
    from 60 are not the same object, and the UI refuses to pretend otherwise.

There is also an optional structural step (Guerre-Perrigne-Vuong): invert the
first-order condition to recover rivals' unobserved *costs* from their observed
bids. See gpv_pseudo_costs().
"""

import math
import random

from .. import db
from . import stats


# --------------------------------------------------------------- bucketing

VALUE_BANDS = [
    ("< Rs 25 L", 0, 2_500_000),
    ("Rs 25 L - 1 Cr", 2_500_000, 10_000_000),
    ("Rs 1 - 5 Cr", 10_000_000, 50_000_000),
    ("Rs 5 - 25 Cr", 50_000_000, 250_000_000),
    ("> Rs 25 Cr", 250_000_000, float("inf")),
]


def value_band(value):
    for label, lo, hi in VALUE_BANDS:
        if lo <= value < hi:
            return label
    return VALUE_BANDS[-1][0]


def _band_bounds(label):
    for name, lo, hi in VALUE_BANDS:
        if name == label:
            return lo, hi
    return 0, float("inf")


def _fetch_ratios(where_sql, params):
    """
    Pull (bid ratio, bidder count, tender id, bidder, rank) rows for a filter.
    One row per historical bid, not per tender.
    """
    sql = f"""
        SELECT b.amount * 1.0 / a.estimated_value AS ratio,
               a.n_bidders                        AS n_bidders,
               a.id                               AS award_id,
               a.estimated_value                  AS estimated_value,
               b.bidder                           AS bidder,
               b.rank                             AS rank
        FROM bids b
        JOIN awards a ON a.id = b.award_id
        WHERE {where_sql}
    """
    return db.query(sql, params)


def _bucket_rows(buyer=None, category=None, band=None):
    where, params = ["1=1"], []
    if buyer:
        where.append("a.buyer = ?")
        params.append(buyer)
    if category:
        where.append("a.category = ?")
        params.append(category)
    if band:
        lo, hi = _band_bounds(band)
        where.append("a.estimated_value >= ? AND a.estimated_value < ?")
        params.extend([lo, hi if hi != float("inf") else 1e18])
    return _fetch_ratios(" AND ".join(where), params)


# ------------------------------------------------------------- the model

class PricingModel:
    """
    Holds the estimated rival-bid distribution for one tender and answers
    questions about it: win probability, expected profit, optimal bid.
    """

    def __init__(self, buyer, category, estimated_value):
        self.buyer = buyer
        self.category = category
        self.value = float(estimated_value)
        self.band = value_band(self.value)

        # Bucket hierarchy: the tightest match we can find becomes the sample,
        # the next level up becomes the prior we shrink toward.
        exact = _bucket_rows(buyer, category, self.band)
        buyer_cat = _bucket_rows(buyer, category, None)
        cat_band = _bucket_rows(None, category, self.band)
        cat_only = _bucket_rows(None, category, None)
        everything = _bucket_rows(None, None, None)

        if len(exact) >= 3:
            rows, prior_rows = exact, buyer_cat or cat_band or cat_only
            self.bucket = f"{buyer} / {category} / {self.band}"
            self.prior_label = f"{category} (all buyers)"
        elif len(buyer_cat) >= 3:
            rows, prior_rows = buyer_cat, cat_only
            self.bucket = f"{buyer} / {category}"
            self.prior_label = f"{category} (all buyers)"
        elif len(cat_band) >= 3:
            rows, prior_rows = cat_band, cat_only
            self.bucket = f"{category} / {self.band}"
            self.prior_label = f"{category} (all bands)"
        elif cat_only:
            rows, prior_rows = cat_only, everything
            self.bucket = f"{category} (all buyers)"
            self.prior_label = "All tenders"
        else:
            rows, prior_rows = everything, []
            self.bucket = "All tenders (no category history)"
            self.prior_label = None

        self.rows = rows
        self.prior_rows = [r for r in prior_rows if r not in rows] or prior_rows

        self.ratios = [r["ratio"] for r in rows]
        self.prior_ratios = [r["ratio"] for r in self.prior_rows]

        # Distinct tenders behind the sample -- the honest "n", since 40 bids
        # from 5 tenders is 5 observations of a buyer's behaviour, not 40.
        self.n_tenders = len({r["award_id"] for r in rows})
        self.n_bids = len(rows)

        self.G = stats.ECDF(self.ratios, self.prior_ratios, k=8.0)

        # Empirical distribution of how many bidders show up.
        counts = {}
        for aid in {r["award_id"] for r in rows}:
            n = next(r["n_bidders"] for r in rows if r["award_id"] == aid)
            counts[n] = counts.get(n, 0) + 1
        if not counts:
            counts = {4: 1}
        total = sum(counts.values())
        # We would be one of the bidders, so rivals = n - 1.
        self.rival_counts = {max(1, n - 1): c / total for n, c in counts.items()}
        self.expected_rivals = sum(n * p for n, p in self.rival_counts.items())

        # Winning (L1) ratios, for context on the screen.
        l1 = [r["ratio"] for r in rows if r["rank"] == 1]
        self.l1_ratios = sorted(l1)

    # ---------------------------------------------------------- core curves

    def win_prob(self, b, G=None, rival_counts=None):
        """P(no rival bids below b), averaged over the bidder-count distribution."""
        G = G or self.G
        rival_counts = rival_counts or self.rival_counts
        s = G.survival(b)
        s = stats.clamp(s, 0.0, 1.0)
        return sum(p * (s ** n) for n, p in rival_counts.items())

    def expected_profit(self, b, cost_ratio, G=None, rival_counts=None):
        return (b - cost_ratio) * self.value * self.win_prob(b, G, rival_counts)

    def _grid(self, cost_ratio, points=161):
        """Search grid: deliberately wide, so the argmax cannot be clipped."""
        lo = max(cost_ratio, 0.35)
        hi = max(lo + 0.02, 1.25)
        return stats.linspace(lo, hi, points)

    def _display_grid(self, cost_ratio, points=161):
        """
        Plotting grid: trimmed at the point where winning becomes essentially
        impossible. Past there the curve is a flat line at zero, and drawing it
        squashes the peak -- the only part of the picture anyone reads -- into
        the left margin. The optimum is always found on the full grid above.
        """
        lo = max(cost_ratio, 0.35)
        full_hi = max(lo + 0.02, 1.25)
        hi = full_hi
        for b in stats.linspace(lo, full_hi, 80):
            if self.win_prob(b) < 0.004:
                hi = min(full_hi, b + (b - lo) * 0.18)
                break
        hi = max(hi, lo * 1.05, lo + 0.02)
        return stats.linspace(lo, hi, points)

    def profit_curve(self, cost_ratio, points=161):
        grid = self._display_grid(cost_ratio, points)
        return [
            {
                "bid_ratio": b,
                "bid": b * self.value,
                "win_prob": self.win_prob(b),
                "expected_profit": self.expected_profit(b, cost_ratio),
                "margin": (b - cost_ratio) * self.value,
            }
            for b in grid
        ]

    def optimal_ratio(self, cost_ratio, G=None, rival_counts=None, points=161):
        best_b, best_v = None, -float("inf")
        for b in self._grid(cost_ratio, points):
            v = self.expected_profit(b, cost_ratio, G, rival_counts)
            if v > best_v:
                best_b, best_v = b, v
        return best_b, best_v

    def ratio_for_win_prob(self, target, lo=0.3, hi=1.4):
        """Invert the win-probability curve by bisection."""
        for _ in range(60):
            mid = (lo + hi) / 2
            if self.win_prob(mid) > target:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2

    # --------------------------------------------------------- uncertainty

    def bootstrap_optimum(self, cost_ratio, reps=160, seed=11):
        """
        Resample the historical bids (and the bidder counts) and re-solve.
        The spread of the resulting optimal bids is the honest uncertainty in
        the recommendation -- not a decoration, the main output.
        """
        if self.n_bids < 2:
            return {"reps": 0, "bid_ratio_ci": None, "profit_ci": None,
                    "win_prob_ci": None, "samples": []}
        rng = random.Random(seed)
        sample = self.ratios
        prior = self.prior_ratios
        n = len(sample)
        counts = list(self.rival_counts.items())

        opt_ratios, opt_profits, opt_winp = [], [], []
        for _ in range(reps):
            rs = [sample[rng.randrange(n)] for _ in range(n)]
            G = stats.ECDF(rs, prior, k=8.0)
            # Resample the bidder-count distribution too (multinomial-ish).
            rc = {}
            for _ in range(max(3, len(counts))):
                nrivals, _p = counts[rng.randrange(len(counts))]
                rc[nrivals] = rc.get(nrivals, 0) + 1
            tot = sum(rc.values())
            rc = {k: v / tot for k, v in rc.items()}

            b, v = self.optimal_ratio(cost_ratio, G, rc, points=81)
            if b is None:
                continue
            opt_ratios.append(b)
            opt_profits.append(v)
            opt_winp.append(self.win_prob(b, G, rc))

        return {
            "reps": len(opt_ratios),
            "bid_ratio_ci": stats.ci(opt_ratios, 0.90),
            "profit_ci": stats.ci(opt_profits, 0.90),
            "win_prob_ci": stats.ci(opt_winp, 0.90),
            "samples": sorted(opt_ratios),
        }

    def confidence_label(self):
        """
        Data adequacy, stated in words, because 'n=4' means nothing to an
        MSME owner and 'do not trust this number' means a lot.
        """
        n = self.n_tenders
        if n >= 25:
            return ("high", f"{n} comparable tenders in this bucket. "
                            "The distribution is well pinned down.")
        if n >= 10:
            return ("medium", f"{n} comparable tenders. Usable, but treat the "
                              "interval as the answer, not the point estimate.")
        if n >= 4:
            return ("low", f"Only {n} comparable tenders. The recommendation is "
                           "shrunk heavily toward the category average and the "
                           "interval is wide. Sanity-check against your own costing.")
        return ("very low", f"{n} comparable tenders. There is not enough history "
                            "here to price from. Shown for completeness only -- "
                            "do not bid off this number.")

    # ------------------------------------------------- structural extension

    def gpv_pseudo_costs(self, max_points=250):
        """
        Guerre-Perrigne-Vuong step two, adapted to procurement.

        A rational bidder with cost c facing (n-1) opponents maximises
        (b - c)(1 - G(b))^(n-1). The first-order condition rearranges to

            c = b - (1 - G(b)) / ((n - 1) * g(b))

        so with nonparametric estimates of G (empirical CDF) and g (kernel
        density) we can recover each rival's implied cost from the bid they
        actually submitted. Two uses: it tells you whether a bucket is
        competitive (bids near cost) or fat (bids far above cost), and a
        recovered cost *below* a plausible floor is a red flag worth a look.
        """
        rows = self.rows[:max_points]
        if len(rows) < 8:
            return {"available": False, "reason": "needs at least 8 historical bids"}

        xs = [r["ratio"] for r in rows]
        h = stats.silverman_bandwidth(xs)
        G = stats.ECDF(xs, [], k=0.0)

        recovered, markups = [], []
        for r in rows:
            b = r["ratio"]
            opponents = max(1, r["n_bidders"] - 1)
            g = stats.kde(xs, b, h)
            if g <= 1e-9:
                continue
            c = b - (1 - G.cdf(b)) / (opponents * g)
            if not math.isfinite(c) or c <= 0 or c > b:
                continue                       # FOC violated at the tails
            recovered.append(c)
            markups.append((b - c) / c)

        if len(recovered) < 5:
            return {"available": False,
                    "reason": "first-order condition unstable on this sample"}

        srt = sorted(recovered)
        msrt = sorted(markups)
        return {
            "available": True,
            "n": len(recovered),
            "bandwidth": h,
            "cost_ratio_median": stats.quantile(srt, 0.5),
            "cost_ratio_p10": stats.quantile(srt, 0.10),
            "cost_ratio_p90": stats.quantile(srt, 0.90),
            "markup_median": stats.quantile(msrt, 0.5),
            "interpretation": _markup_reading(stats.quantile(msrt, 0.5)),
        }


def _markup_reading(m):
    if m < 0.03:
        return ("Rivals are bidding essentially at cost. This bucket is brutally "
                "competitive -- winning here means winning work worth little.")
    if m < 0.08:
        return ("Thin recovered markups. Competitive bucket; your cost base has "
                "to be genuinely better to profit here.")
    if m < 0.18:
        return ("Normal recovered markups for public works. There is room to "
                "price to a target rather than to the floor.")
    return ("Fat recovered markups relative to implied costs. Either few real "
            "bidders, high risk loading, or worth a look at the cartel screens.")


# ------------------------------------------------------------- model cache

_MODEL_CACHE = {}


def get_model(buyer, category, estimated_value):
    """
    Cached model lookup. The distribution depends only on the bucket, not on
    the individual tender, so ranking 200 live tenders costs a handful of
    queries instead of a thousand. Call clear_cache() after ingesting data.
    """
    key = (buyer, category, value_band(float(estimated_value)))
    model = _MODEL_CACHE.get(key)
    if model is None:
        model = PricingModel(buyer, category, estimated_value)
        _MODEL_CACHE[key] = model
    # The bucket is shared; the tender's own value is not.
    model.value = float(estimated_value)
    return model


def clear_cache():
    _MODEL_CACHE.clear()


def quick_score(buyer, category, estimated_value, cost_ratio):
    """
    Cheap ranking score for list views: optimal bid and expected profit with
    no bootstrap. Used by the dashboard and the portfolio optimiser.
    """
    model = get_model(buyer, category, estimated_value)
    b, profit = model.optimal_ratio(cost_ratio, points=81)
    if b is None:
        return None
    return {
        "bid_ratio": b,
        "bid": b * model.value,
        "win_prob": model.win_prob(b),
        "expected_profit": profit,
        "margin": (b - cost_ratio) * model.value,
        "expected_rivals": model.expected_rivals,
        "n_tenders": model.n_tenders,
        "confidence": model.confidence_label()[0],
    }


# ------------------------------------------------------------- public API

def recommend(buyer, category, estimated_value, cost, target_margin_pct=8.0,
              curve_points=121, bootstrap_reps=160):
    """
    The full recommendation object the UI renders.

    cost is the bidder's own all-in cost estimate in rupees -- the one input
    the model genuinely cannot infer for them.
    """
    model = get_model(buyer, category, estimated_value)
    V = model.value
    cost_ratio = cost / V if V else 0.0

    b_star, profit_star = model.optimal_ratio(cost_ratio, points=241)
    boot = model.bootstrap_optimum(cost_ratio, reps=bootstrap_reps)
    level, note = model.confidence_label()

    # Three framings of the same curve, because "one number" hides the
    # trade-off the bidder actually cares about. Every option is floored at
    # cost: a bid below your own cost is not a strategy, and presenting one
    # as "aggressive" would be the kind of confident nonsense this tool exists
    # to avoid.
    floor = cost_ratio * 1.001

    def pack(b, label, rationale, target_win=None):
        clipped = b < floor
        b = max(b, floor)
        wp = model.win_prob(b)
        note = rationale
        if clipped and target_win is not None:
            note = (f"Winning {target_win:.0%} of these would mean bidding below "
                    f"your own cost in this bucket. Floored at breakeven, where "
                    f"you win {wp:.0%} of the time. If you need this work, the "
                    f"cost base has to come down -- the price cannot.")
        return {
            "label": label,
            "bid_ratio": b,
            "bid": b * V,
            "win_prob": wp,
            "margin": (b - cost_ratio) * V,
            "margin_pct": ((b - cost_ratio) / cost_ratio * 100) if cost_ratio else 0.0,
            "expected_profit": model.expected_profit(b, cost_ratio),
            "floored_at_cost": clipped,
            "rationale": note,
        }

    options = [
        pack(model.ratio_for_win_prob(0.25), "Conservative",
             "Priced for margin. You win roughly one in four of these, but the "
             "ones you win are worth having.", 0.25),
        pack(b_star, "Recommended",
             "Maximises expected profit: the point where a rupee of extra margin "
             "stops paying for the win probability it costs you."),
        pack(model.ratio_for_win_prob(0.70), "Aggressive",
             "Priced to win. Use when you need throughput, an empanelment, or "
             "the crew is otherwise idle.", 0.70),
    ]
    options.sort(key=lambda o: o["bid_ratio"], reverse=True)

    target_ratio = cost_ratio * (1 + target_margin_pct / 100.0)

    return {
        "tender": {
            "buyer": buyer,
            "category": category,
            "estimated_value": V,
            "value_band": model.band,
        },
        "inputs": {
            "cost": cost,
            "cost_ratio": cost_ratio,
            "target_margin_pct": target_margin_pct,
        },
        "data": {
            "bucket": model.bucket,
            "prior": model.prior_label,
            "n_tenders": model.n_tenders,
            "n_bids": model.n_bids,
            "shrinkage_weight": model.G.weight,
            "effective_n": model.G.effective_n(),
            "expected_rivals": model.expected_rivals,
            "confidence": level,
            "confidence_note": note,
            "l1_ratio_median": stats.quantile(model.l1_ratios, 0.5) if model.l1_ratios else None,
            "l1_ratio_p10": stats.quantile(model.l1_ratios, 0.10) if model.l1_ratios else None,
            "l1_ratio_p90": stats.quantile(model.l1_ratios, 0.90) if model.l1_ratios else None,
        },
        "recommendation": {
            "bid": b_star * V if b_star else None,
            "bid_ratio": b_star,
            "expected_profit": profit_star,
            "win_prob": model.win_prob(b_star) if b_star else None,
            "margin": (b_star - cost_ratio) * V if b_star else None,
            "bid_ci": [c * V for c in boot["bid_ratio_ci"]] if boot["bid_ratio_ci"] else None,
            "profit_ci": boot["profit_ci"],
            "win_prob_ci": boot["win_prob_ci"],
            "bootstrap_reps": boot["reps"],
        },
        "options": options,
        "breakeven": {
            "bid": cost,
            "bid_ratio": cost_ratio,
            "win_prob_at_breakeven": model.win_prob(cost_ratio),
        },
        "target_margin_bid": {
            "bid": target_ratio * V,
            "bid_ratio": target_ratio,
            "win_prob": model.win_prob(target_ratio),
            "expected_profit": model.expected_profit(target_ratio, cost_ratio),
        },
        "curve": model.profit_curve(cost_ratio, curve_points),
        "structural": model.gpv_pseudo_costs(),
    }


def evaluate_bid(buyer, category, estimated_value, cost, bid):
    """What-if for a bid the user types in themselves."""
    model = get_model(buyer, category, estimated_value)
    V = model.value
    cost_ratio = cost / V if V else 0.0
    b = bid / V if V else 0.0
    b_star, profit_star = model.optimal_ratio(cost_ratio, points=241)
    ep = model.expected_profit(b, cost_ratio)
    return {
        "bid": bid,
        "bid_ratio": b,
        "win_prob": model.win_prob(b),
        "margin": (b - cost_ratio) * V,
        "margin_pct": ((b - cost_ratio) / cost_ratio * 100) if cost_ratio else 0,
        "expected_profit": ep,
        "optimal_bid": b_star * V if b_star else None,
        "optimal_expected_profit": profit_star,
        "money_left_on_table": (profit_star - ep) if b_star else None,
        "verdict": _verdict(b, b_star, cost_ratio),
    }


def _verdict(b, b_star, cost_ratio):
    if b_star is None:
        return "No historical data to judge this against."
    if b < cost_ratio:
        return "Below your own cost. This is a loss-making bid however it lands."
    d = (b - b_star) / b_star
    if d > 0.06:
        return ("Priced well above the expected-profit optimum. Higher margin if "
                "it lands, but it probably will not land.")
    if d < -0.06:
        return ("Priced below the optimum -- you are buying win probability that "
                "was already close to bought. Expected profit falls.")
    return "Within a few percent of the expected-profit optimum."
