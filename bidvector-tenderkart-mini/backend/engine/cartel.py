"""
Bid-rigging screens.

These are the standard structural screens competition authorities run over
procurement data (the OECD Guidelines for Fighting Bid Rigging and the
screening literature that followed Porter & Zona). None of them prove
anything. What they do is rank buckets by how far their bid patterns sit from
what independent competitive bidding would produce, so a human knows where to
look.

The screens implemented here:

  1. Within-tender dispersion.  Independent bidders costing the same job from
     different books disagree. A bucket where the coefficient of variation of
     bids is persistently tiny is either a commodity with no cost variation or
     a set of bids drawn up by the same person.

  2. L1-L2 spread.  Under competition the winning margin shrinks as bidders
     are added. A bucket where the winner consistently beats the runner-up by
     a wide, stable margin while the losers cluster tightly above is the
     classic cover-bidding signature.

  3. Win concentration and rotation.  Cartels allocate. Concentrated wins are
     unremarkable on their own -- one firm may simply be better -- but wins
     that rotate cleanly among a fixed set, with each firm's losing bids
     sitting far above its winning bids, are not.

  4. Repeated pairing.  Firms that always appear in the same tenders more
     often than the bucket's participation rates would predict.

  5. Round-number clustering.  Genuine BoQ-derived bids end in arbitrary
     digits. Negotiated numbers end in zeros.

  6. Benford first-digit conformity.  Weak on small samples, included because
     it is cheap and it is what an auditor will ask about.

Every output carries its sample size and a plain statement of what it does
and does not mean.
"""

import math
from collections import defaultdict

from .. import db
from . import stats


def _bucket_data(buyer=None, category=None, min_bidders=3):
    where, params = ["1=1"], []
    if buyer:
        where.append("a.buyer = ?")
        params.append(buyer)
    if category:
        where.append("a.category = ?")
        params.append(category)
    rows = db.query(f"""
        SELECT a.id, a.ref_no, a.title, a.buyer, a.category, a.estimated_value,
               a.awarded_at, a.n_bidders, a.winner,
               b.bidder, b.amount, b.rank
        FROM awards a JOIN bids b ON b.award_id = a.id
        WHERE {' AND '.join(where)}
        ORDER BY a.awarded_at, a.id, b.rank
    """, params)

    tenders = defaultdict(lambda: {"bids": []})
    for r in rows:
        t = tenders[r["id"]]
        t.update({k: r[k] for k in
                  ("ref_no", "title", "buyer", "category", "estimated_value",
                   "awarded_at", "n_bidders", "winner")})
        t["bids"].append({"bidder": r["bidder"], "amount": r["amount"],
                          "rank": r["rank"]})
    return [t for t in tenders.values() if len(t["bids"]) >= min_bidders]


# ------------------------------------------------------------------ screens

def screen_dispersion(tenders):
    cvs = []
    for t in tenders:
        amts = [b["amount"] for b in t["bids"]]
        cvs.append(stats.coefficient_of_variation(amts))
    if not cvs:
        return None
    med = stats.median(cvs)
    tight = sum(1 for c in cvs if c < 0.02) / len(cvs)
    return {
        "name": "Within-tender bid dispersion",
        "value": med,
        "display": f"{med * 100:.1f}% median CV",
        "flag": med < 0.03,
        "severity": "high" if med < 0.02 else ("medium" if med < 0.03 else "low"),
        "detail": (f"{tight * 100:.0f}% of tenders in this bucket had all bids "
                   f"within 2% of each other."),
        "meaning": ("Independent bidders costing the same work disagree by more "
                    "than this. Sustained sub-3% dispersion is worth a look -- "
                    "unless the work is a pure commodity supply where it is "
                    "expected."),
        "n": len(cvs),
    }


def screen_l1_l2_spread(tenders):
    spreads, loser_cvs = [], []
    for t in tenders:
        amts = sorted(b["amount"] for b in t["bids"])
        if len(amts) < 3:
            continue
        spreads.append((amts[1] - amts[0]) / amts[0])
        losers = amts[1:]
        loser_cvs.append(stats.coefficient_of_variation(losers))
    if not spreads:
        return None
    med_spread = stats.median(spreads)
    med_loser_cv = stats.median(loser_cvs) if loser_cvs else 0
    cover = med_spread > 0.05 and med_loser_cv < 0.025
    return {
        "name": "L1-L2 spread vs loser clustering",
        "value": med_spread,
        "display": f"{med_spread * 100:.1f}% median L1-L2 gap",
        "flag": cover,
        "severity": "high" if cover and med_spread > 0.08 else
                    ("medium" if cover else "low"),
        "detail": (f"Losing bids cluster within {med_loser_cv * 100:.1f}% of each "
                   f"other while the winner sits {med_spread * 100:.1f}% below them."),
        "meaning": ("Cover bidding leaves this fingerprint: a comfortable winner "
                    "and a tight, uninterested pack above. Under real competition "
                    "the gap to the runner-up narrows as bidders are added."),
        "n": len(spreads),
    }


def screen_concentration(tenders):
    wins = defaultdict(int)
    appearances = defaultdict(int)
    for t in tenders:
        wins[t["winner"]] += 1
        for b in t["bids"]:
            appearances[b["bidder"]] += 1
    total = sum(wins.values())
    if total < 4:
        return None
    shares = [w / total for w in wins.values()]
    hhi = sum(s * s for s in shares)

    # Hit rate per firm: how often a participant converts to a win.
    conversion = {
        f: wins.get(f, 0) / appearances[f]
        for f in appearances if appearances[f] >= 3
    }
    suspicious = sorted(
        ({"bidder": f, "wins": wins.get(f, 0), "appearances": appearances[f],
          "hit_rate": r} for f, r in conversion.items()),
        key=lambda x: -x["hit_rate"])[:6]

    return {
        "name": "Win concentration",
        "value": hhi,
        "display": f"HHI {hhi:.2f} over {len(wins)} winners",
        "flag": hhi > 0.25,
        "severity": "high" if hhi > 0.4 else ("medium" if hhi > 0.25 else "low"),
        "detail": f"{len(wins)} distinct winners across {total} awards.",
        "meaning": ("Concentration alone is not evidence -- one firm may simply "
                    "be the most efficient. It matters in combination with the "
                    "rotation and pairing screens below."),
        "n": total,
        "table": suspicious,
    }


def screen_rotation(tenders):
    """
    Cartel allocation shows up as each firm's losing bids sitting far above
    its own winning bids: when it is your turn you bid to win, otherwise you
    bid to lose. Under independent competition a firm's bid ratio should not
    depend on whether it happened to win.
    """
    by_firm = defaultdict(lambda: {"win": [], "lose": []})
    for t in tenders:
        ev = t["estimated_value"] or 1
        for b in t["bids"]:
            key = "win" if b["rank"] == 1 else "lose"
            by_firm[b["bidder"]][key].append(b["amount"] / ev)

    rows = []
    for firm, d in by_firm.items():
        if len(d["win"]) >= 2 and len(d["lose"]) >= 3:
            gap = stats.mean(d["lose"]) - stats.mean(d["win"])
            rows.append({"bidder": firm, "wins": len(d["win"]),
                         "losses": len(d["lose"]),
                         "win_ratio": stats.mean(d["win"]),
                         "lose_ratio": stats.mean(d["lose"]),
                         "gap": gap})
    if not rows:
        return None
    rows.sort(key=lambda r: -r["gap"])
    worst = rows[0]
    med_gap = stats.median([r["gap"] for r in rows])
    return {
        "name": "Bid rotation signature",
        "value": med_gap,
        "display": f"{med_gap * 100:.1f}pp median win/lose bid gap",
        "flag": med_gap > 0.06,
        "severity": "high" if med_gap > 0.10 else
                    ("medium" if med_gap > 0.06 else "low"),
        "detail": (f"{worst['bidder']} bids {worst['gap'] * 100:.1f} percentage "
                   f"points higher when it loses than when it wins."),
        "meaning": ("A firm's price should not know in advance whether it is "
                    "going to win. A persistent gap means the losing bids were "
                    "never meant to win."),
        "n": len(rows),
        "table": rows[:6],
    }


def screen_pairing(tenders):
    pair = defaultdict(int)
    appear = defaultdict(int)
    n_t = len(tenders)
    for t in tenders:
        firms = sorted({b["bidder"] for b in t["bids"]})
        for f in firms:
            appear[f] += 1
        for i in range(len(firms)):
            for j in range(i + 1, len(firms)):
                pair[(firms[i], firms[j])] += 1
    if n_t < 6:
        return None

    rows = []
    for (a, b), c in pair.items():
        expected = appear[a] * appear[b] / n_t
        # Lift on a tiny expected count is noise, not signal: two firms meeting
        # 3 times against 0.4 expected is an 8x "lift" that means nothing. We
        # require both a real co-occurrence count and a non-trivial baseline
        # before the ratio is allowed to say anything.
        if c < 4 or expected < 1.5:
            continue
        rows.append({"pair": f"{a} + {b}", "together": c,
                     "expected": expected, "lift": c / expected})
    if not rows:
        return None
    rows.sort(key=lambda r: -r["lift"])
    top = rows[0]
    return {
        "name": "Repeated pairing",
        "value": top["lift"],
        "display": f"{top['lift']:.2f}x expected co-occurrence",
        "flag": top["lift"] > 2.5,
        "severity": "high" if top["lift"] > 3.5 else
                    ("medium" if top["lift"] > 2.5 else "low"),
        "detail": (f"{top['pair']} appeared in {top['together']} of the same "
                   f"tenders against {top['expected']:.1f} expected from their "
                   f"individual participation rates."),
        "meaning": ("Firms that shadow each other into tenders more than chance "
                    "predicts may be coordinating entry. Geography and "
                    "specialisation explain most of this -- check those first."),
        "n": len(rows),
        "table": rows[:6],
    }


def screen_round_numbers(tenders):
    amounts = [b["amount"] for t in tenders for b in t["bids"]]
    if len(amounts) < 20:
        return None
    round_10k = sum(1 for a in amounts if stats.last_digits(a, 4) == 0)
    frac = round_10k / len(amounts)
    return {
        "name": "Round-number clustering",
        "value": frac,
        "display": f"{frac * 100:.1f}% of bids end in 0000",
        "flag": frac > 0.15,
        "severity": "high" if frac > 0.30 else ("medium" if frac > 0.15 else "low"),
        "detail": f"{round_10k} of {len(amounts)} bids are exact multiples of Rs 10,000.",
        "meaning": ("A bid built up from a bill of quantities lands on an "
                    "arbitrary number. A bid agreed in a room lands on a round "
                    "one. Expect roughly 0.01% by chance."),
        "n": len(amounts),
    }


def screen_benford(tenders):
    amounts = [b["amount"] for t in tenders for b in t["bids"]]
    if len(amounts) < 40:
        return None
    observed = [0] * 10
    for a in amounts:
        d = stats.benford_first_digit(a)
        if 1 <= d <= 9:
            observed[d] += 1
    n = sum(observed)
    if n < 40:
        return None
    chi2 = 0.0
    for d in range(1, 10):
        exp = n * math.log10(1 + 1 / d)
        chi2 += (observed[d] - exp) ** 2 / exp
    # 8 df, 95th percentile is 15.51
    return {
        "name": "Benford first-digit conformity",
        "value": chi2,
        "display": f"chi-square {chi2:.1f} (8 df, critical 15.5)",
        "flag": False,
        "severity": "low",
        "informational": True,
        "detail": f"Computed over {n} bid amounts. "
                  + ("Departs from Benford." if chi2 > 15.51 else "Conforms."),
        "meaning": ("Informational only, and deliberately excluded from the risk "
                    "score. Bid amounts are anchored to a published estimate, so "
                    "their first digits are not a naturally Benford population -- "
                    "a departure here is expected and means little. Reported "
                    "because auditors ask for it."),
        "n": n,
    }


# -------------------------------------------------------------------- public

def run_screens(buyer=None, category=None):
    tenders = _bucket_data(buyer, category)
    if len(tenders) < 4:
        return {
            "bucket": _label(buyer, category),
            "n_tenders": len(tenders),
            "insufficient": True,
            "message": ("Fewer than 4 multi-bidder tenders in this bucket. "
                        "Screens are not meaningful here and are not shown."),
            "screens": [],
            "risk": "unknown",
        }

    screens = [s for s in (
        screen_dispersion(tenders),
        screen_l1_l2_spread(tenders),
        screen_concentration(tenders),
        screen_rotation(tenders),
        screen_pairing(tenders),
        screen_round_numbers(tenders),
        screen_benford(tenders),
    ) if s]

    scored = [s for s in screens if not s.get("informational")]
    high = sum(1 for s in scored if s["severity"] == "high")
    med = sum(1 for s in scored if s["severity"] == "medium")
    if high >= 2:
        risk = "elevated"
    elif high >= 1 or med >= 3:
        risk = "watch"
    else:
        risk = "normal"

    return {
        "bucket": _label(buyer, category),
        "n_tenders": len(tenders),
        "insufficient": False,
        "screens": screens,
        "flags_high": high,
        "flags_medium": med,
        "risk": risk,
        "disclaimer": (
            "These are statistical screens, not findings. Every pattern here "
            "has an innocent explanation that is usually the correct one -- "
            "specialisation, geography, a genuinely cheaper firm. They are for "
            "deciding where to look, and for deciding whether a bucket is worth "
            "your bid cost at all."
        ),
    }


def rank_buckets(limit=12):
    """Run the screens across every buyer x category bucket and rank by risk."""
    combos = db.query("""
        SELECT buyer, category, COUNT(*) AS n
        FROM awards GROUP BY buyer, category HAVING n >= 5
    """)
    out = []
    for c in combos:
        r = run_screens(c["buyer"], c["category"])
        if r.get("insufficient"):
            continue
        out.append({
            "buyer": c["buyer"],
            "category": c["category"],
            "n_tenders": r["n_tenders"],
            "risk": r["risk"],
            "flags_high": r["flags_high"],
            "flags_medium": r["flags_medium"],
            "top_flag": next((s["name"] for s in r["screens"]
                              if s["severity"] == "high"), None),
        })
    order = {"elevated": 0, "watch": 1, "normal": 2}
    out.sort(key=lambda x: (order[x["risk"]], -x["flags_high"], -x["flags_medium"]))
    return out[:limit]


def _label(buyer, category):
    if buyer and category:
        return f"{buyer} / {category}"
    if buyer:
        return buyer
    if category:
        return category
    return "All tenders"
