"""
EMD-constrained portfolio selection.

The observation that makes this worth building: for an MSME, bidding is not a
per-tender decision. Every bid locks Earnest Money Deposit -- typically 2% of
the estimated value -- in a demand draft or bank guarantee until the tender is
decided, which can take months. Working capital is the binding constraint, not
enthusiasm.

So "which tenders should I bid on this month?" is a constrained allocation
problem:

    maximise   sum_i  E[profit_i] * x_i
    subject to sum_i  EMD_i * x_i  <=  working capital
               sum_i  x_i          <=  bids the team can actually prepare
               x_i in {0, 1}

That is a two-constraint 0/1 knapsack. We solve it exactly with dynamic
programming (EMD discretised to Rs 10,000 units, which is finer than any real
EMD figure), and we report the shadow price of capital -- what the next
Rs 1 lakh of working capital would be worth in expected profit. That number
tells an owner whether an overdraft facility pays for itself, which is a more
useful thing than a list of tenders sorted by deadline.

We also run a greedy profit-per-rupee-of-EMD baseline so the UI can show what
the optimiser bought you over the obvious heuristic.
"""

UNIT = 10_000          # Rs 10k discretisation for the DP


def _weight(x):
    """
    Item weights round UP. Rounding to the nearest unit lets a set of items
    whose true EMD exceeds the capital look feasible to the DP -- the
    solution comes back over budget in actual rupees, which is worse than
    useless when the constraint is money you do not have.
    """
    return -(-int(x) // UNIT) if x > 0 else 0


def _capacity(x):
    """Capacity rounds DOWN, for the same reason from the other side."""
    return int(x // UNIT)


def optimise(candidates, capital, max_bids=None):
    """
    candidates: list of dicts with at least
        id, title, emd, expected_profit  (win_prob optional, carried through)
    capital:    rupees of working capital available to lock in EMD
    max_bids:   optional cap on how many bids the team can prepare

    Returns the chosen set, the totals, the greedy comparison and the shadow
    price of capital.
    """
    items = [c for c in candidates if c.get("expected_profit", 0) > 0]
    if not items or capital <= 0:
        return _empty(capital, candidates)

    cap_units = _capacity(capital)
    if cap_units <= 0:
        return _empty(capital, candidates)

    n = len(items)
    max_bids = max_bids if max_bids and max_bids > 0 else n
    max_bids = min(max_bids, n)

    weights = [_weight(it.get("emd", 0)) for it in items]
    values = [float(it["expected_profit"]) for it in items]

    best, keep = _dp(n, weights, values, cap_units, max_bids)
    chosen_idx = _traceback(keep, n, cap_units, max_bids, weights)

    chosen = [items[i] for i in chosen_idx]
    total_emd = sum(it.get("emd", 0) for it in chosen)
    total_profit = sum(it["expected_profit"] for it in chosen)

    # Shadow price: what one more lakh of capital would buy.
    bump = _capacity(100_000)
    best_more, _ = _dp(n, weights, values, cap_units + bump, max_bids)
    shadow = max(0.0, best_more - best)

    greedy = _greedy(items, capital, max_bids)

    rejected = [
        {
            "id": it.get("id"),
            "title": it.get("title"),
            "emd": it.get("emd", 0),
            "expected_profit": it["expected_profit"],
            "profit_per_emd_rupee": (it["expected_profit"] / it["emd"]) if it.get("emd") else None,
            "reason": _reject_reason(it, chosen, capital),
        }
        for i, it in enumerate(items) if i not in set(chosen_idx)
    ]
    rejected.sort(key=lambda r: -(r["profit_per_emd_rupee"] or 0))

    return {
        "capital": capital,
        "max_bids": max_bids,
        "selected": [_pack(it) for it in chosen],
        "rejected": rejected[:12],
        "totals": {
            "emd_committed": total_emd,
            "capital_utilisation": total_emd / capital if capital else 0,
            "expected_profit": total_profit,
            "bids": len(chosen),
            "return_on_locked_capital": (total_profit / total_emd) if total_emd else None,
        },
        "greedy_baseline": {
            "expected_profit": greedy["profit"],
            "bids": greedy["count"],
            "uplift_from_optimiser": total_profit - greedy["profit"],
        },
        "shadow_price": {
            "per_lakh": shadow,
            "note": _shadow_note(shadow),
        },
    }


def _dp(n, weights, values, cap_units, max_bids):
    """
    Two-constraint 0/1 knapsack. dp[c][k] = best value using capacity <= c
    and at most k items. Keep-flags are stored per item for traceback.
    """
    NEG = float("-inf")
    dp = [[0.0] * (max_bids + 1) for _ in range(cap_units + 1)]
    keep = []
    for i in range(n):
        w, v = weights[i], values[i]
        take = [[False] * (max_bids + 1) for _ in range(cap_units + 1)]
        # Iterate capacity downward so each item is used at most once.
        for c in range(cap_units, -1, -1):
            for k in range(max_bids, 0, -1):
                if w <= c:
                    cand = dp[c - w][k - 1] + v
                    if cand > dp[c][k]:
                        dp[c][k] = cand
                        take[c][k] = True
        keep.append(take)
    return dp[cap_units][max_bids], keep


def _traceback(keep, n, cap_units, max_bids, weights):
    chosen = []
    c, k = cap_units, max_bids
    for i in range(n - 1, -1, -1):
        if k > 0 and keep[i][c][k]:
            chosen.append(i)
            c -= weights[i]
            k -= 1
    return list(reversed(chosen))


def _greedy(items, capital, max_bids):
    ranked = sorted(
        items,
        key=lambda it: (it["expected_profit"] / it["emd"]) if it.get("emd") else 1e18,
        reverse=True,
    )
    spent, profit, count = 0.0, 0.0, 0
    for it in ranked:
        emd = it.get("emd", 0)
        if count >= max_bids or spent + emd > capital:
            continue
        spent += emd
        profit += it["expected_profit"]
        count += 1
    return {"profit": profit, "count": count, "spent": spent}


def _pack(it):
    return {
        "id": it.get("id"),
        "title": it.get("title"),
        "buyer": it.get("buyer"),
        "closes_at": it.get("closes_at"),
        "emd": it.get("emd", 0),
        "estimated_value": it.get("estimated_value"),
        "recommended_bid": it.get("recommended_bid"),
        "win_prob": it.get("win_prob"),
        "expected_profit": it["expected_profit"],
        "profit_per_emd_rupee": (it["expected_profit"] / it["emd"]) if it.get("emd") else None,
    }


def _reject_reason(it, chosen, capital):
    if not chosen:
        return "Nothing fits the capital available."
    worst = min(
        (c["expected_profit"] / c["emd"]) if c.get("emd") else 1e18 for c in chosen
    )
    mine = (it["expected_profit"] / it["emd"]) if it.get("emd") else 1e18
    if mine < worst:
        return ("Lower expected profit per rupee of EMD than everything the "
                "optimiser kept.")
    return "Displaced by the bid-count limit rather than by capital."


def _shadow_note(shadow):
    if shadow <= 0:
        return ("Capital is not the binding constraint right now -- the bid-count "
                "limit or the shortage of good tenders is. More overdraft would "
                "buy you nothing this cycle.")
    return (f"An extra Rs 1 lakh of working capital would add about "
            f"Rs {shadow:,.0f} of expected profit this cycle. Compare that "
            f"against your cost of borrowing before taking the facility.")


def _empty(capital, candidates):
    return {
        "capital": capital,
        "max_bids": 0,
        "selected": [],
        "rejected": [],
        "totals": {"emd_committed": 0, "capital_utilisation": 0,
                   "expected_profit": 0, "bids": 0,
                   "return_on_locked_capital": None},
        "greedy_baseline": {"expected_profit": 0, "bids": 0,
                            "uplift_from_optimiser": 0},
        "shadow_price": {"per_lakh": 0, "note": "No priceable candidates."},
    }
