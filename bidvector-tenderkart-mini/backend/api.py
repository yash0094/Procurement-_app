"""
REST API.

Route map:

  auth        POST   /api/auth/register       create account + profile
              POST   /api/auth/login          bearer token
              GET    /api/me                  account + profile
              PUT    /api/profile             update company profile

  discovery   GET    /api/filters             facet values for the filter bar
              GET    /api/tenders             search with filters + eligibility
              GET    /api/tenders/{id}        detail + parsed criteria + checklist
              POST   /api/parse               run the clause parser on pasted text

  pricing     POST   /api/pricing/recommend   the BidVector recommendation
              POST   /api/pricing/evaluate    what-if on a bid you type in

  pipeline    GET    /api/pipeline            your bids and their status
              POST   /api/pipeline            add/update a tender in the pipeline
              DELETE /api/pipeline/{id}       remove

  portfolio   POST   /api/portfolio/optimise  EMD-constrained bid selection

  intel       GET    /api/competitors         who bids in a bucket and how
              GET    /api/competitors/{name}  one firm's bidding behaviour
              GET    /api/screens             collusion screens for a bucket
              GET    /api/screens/ranked      buckets ranked by screen flags

  misc        GET    /api/dashboard           headline metrics + ranked list
              GET    /api/alerts              alert feed
              POST   /api/alerts/{id}/read    mark read
              GET    /api/export/tenders      CSV of the current search
"""

import json
from datetime import datetime, date

from . import db
from .auth import hash_password, verify_password, make_token, read_token
from .server import route, HttpError
from .engine import pricing, eligibility, portfolio, cartel

TODAY = date(2026, 9, 17)      # fixed "today" so the seeded demo stays coherent


# ------------------------------------------------------------------ helpers

def current_user(req, required=True):
    payload = read_token(req.bearer())
    if not payload:
        if required:
            raise HttpError(401, "Sign in to continue")
        return None
    user = db.query_one("SELECT id,email,company_name FROM users WHERE id=?",
                        (payload["uid"],))
    if not user and required:
        raise HttpError(401, "Account no longer exists")
    return user


def get_profile(user_id):
    p = db.query_one("SELECT * FROM profiles WHERE user_id=?", (user_id,))
    if not p:
        return {}
    for k in ("certifications", "states", "categories"):
        p[k] = db.jloads(p.get(k), [])
    return p


def days_left(closes_at):
    try:
        d = datetime.strptime(closes_at, "%Y-%m-%d").date()
        return (d - TODAY).days
    except (ValueError, TypeError):
        return None


def tender_public(row):
    row = dict(row)
    row["eligibility"] = db.jloads(row.pop("eligibility_json", "{}"), {})
    row["days_left"] = days_left(row["closes_at"])
    return row


# --------------------------------------------------------------------- auth

@route("POST", "/api/auth/register")
def register(req):
    email = (req.require("email") or "").strip().lower()
    password = req.require("password")
    company = req.require("company_name")
    if len(password) < 8:
        raise HttpError(400, "Password must be at least 8 characters")
    if db.query_one("SELECT id FROM users WHERE email=?", (email,)):
        raise HttpError(409, "An account with that email already exists")

    uid = db.execute(
        "INSERT INTO users (email,password_hash,company_name,created_at) "
        "VALUES (?,?,?,?)",
        (email, hash_password(password), company, TODAY.isoformat()))

    p = req.json("profile", {}) or {}
    db.execute("""
        INSERT INTO profiles (user_id,udyam_no,msme_class,bidder_class,
            turnover_cr,experience_years,max_similar_work_cr,certifications,
            states,categories,working_capital_cr,overhead_pct,target_margin_pct)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        uid, p.get("udyam_no", ""), p.get("msme_class", "Micro"),
        p.get("bidder_class", "Class II"), float(p.get("turnover_cr") or 0),
        float(p.get("experience_years") or 0),
        float(p.get("max_similar_work_cr") or 0),
        json.dumps(p.get("certifications") or ["GST", "PAN"]),
        json.dumps(p.get("states") or []),
        json.dumps(p.get("categories") or []),
        float(p.get("working_capital_cr") or 0.25),
        float(p.get("overhead_pct") or 12), float(p.get("target_margin_pct") or 8)))

    return {"token": make_token(uid, email),
            "user": {"id": uid, "email": email, "company_name": company}}, 201


@route("POST", "/api/auth/login")
def login(req):
    email = (req.require("email") or "").strip().lower()
    password = req.require("password")
    user = db.query_one("SELECT * FROM users WHERE email=?", (email,))
    if not user or not verify_password(password, user["password_hash"]):
        raise HttpError(401, "Email or password is incorrect")
    return {"token": make_token(user["id"], user["email"]),
            "user": {"id": user["id"], "email": user["email"],
                     "company_name": user["company_name"]}}


@route("GET", "/api/me")
def me(req):
    user = current_user(req)
    return {"user": user, "profile": get_profile(user["id"])}


@route("PUT", "/api/profile")
def update_profile(req):
    user = current_user(req)
    b = req.body or {}
    fields = ["udyam_no", "msme_class", "bidder_class", "turnover_cr",
              "experience_years", "max_similar_work_cr", "working_capital_cr",
              "overhead_pct", "target_margin_pct"]
    sets, params = [], []
    for f in fields:
        if f in b:
            sets.append(f"{f}=?")
            params.append(b[f])
    for f in ["certifications", "states", "categories"]:
        if f in b:
            sets.append(f"{f}=?")
            params.append(json.dumps(b[f]))
    if b.get("company_name"):
        db.execute("UPDATE users SET company_name=? WHERE id=?",
                   (b["company_name"], user["id"]))
    if sets:
        params.append(user["id"])
        db.execute(f"UPDATE profiles SET {','.join(sets)} WHERE user_id=?", params)
    return {"profile": get_profile(user["id"])}


# ---------------------------------------------------------------- discovery

@route("GET", "/api/filters")
def filters(req):
    return {
        "buyers": [r["buyer"] for r in db.query(
            "SELECT DISTINCT buyer FROM tenders ORDER BY buyer")],
        "categories": [r["category"] for r in db.query(
            "SELECT DISTINCT category FROM tenders ORDER BY category")],
        "states": [r["buyer_state"] for r in db.query(
            "SELECT DISTINCT buyer_state FROM tenders ORDER BY buyer_state")],
        "portals": [r["portal"] for r in db.query(
            "SELECT DISTINCT portal FROM tenders ORDER BY portal")],
        "value_bands": [b[0] for b in pricing.VALUE_BANDS],
    }


def _search_rows(req):
    where, params = ["1=1"], []
    q = req.arg("q")
    if q:
        where.append("(LOWER(title) LIKE ? OR LOWER(buyer) LIKE ? "
                     "OR LOWER(ref_no) LIKE ? OR LOWER(description) LIKE ?)")
        like = f"%{q.lower()}%"
        params += [like, like, like, like]
    for field, arg in (("buyer", "buyer"), ("category", "category"),
                       ("buyer_state", "state"), ("portal", "portal")):
        v = req.arg(arg)
        if v:
            where.append(f"{field}=?")
            params.append(v)
    mn = req.arg("min_value", cast=float)
    mx = req.arg("max_value", cast=float)
    if mn:
        where.append("estimated_value >= ?")
        params.append(mn)
    if mx:
        where.append("estimated_value <= ?")
        params.append(mx)
    within = req.arg("closing_within", cast=int)
    if within:
        cutoff = (TODAY.toordinal() + within)
        where.append("julianday(closes_at) - julianday(?) <= ?")
        params += [TODAY.isoformat(), within]
        _ = cutoff
    if req.arg("open_only", "1") == "1":
        where.append("julianday(closes_at) >= julianday(?)")
        params.append(TODAY.isoformat())

    sort = req.arg("sort", "closing")
    order = {
        "closing": "closes_at ASC",
        "value_desc": "estimated_value DESC",
        "value_asc": "estimated_value ASC",
        "newest": "published_at DESC",
    }.get(sort, "closes_at ASC")

    return db.query(
        f"SELECT * FROM tenders WHERE {' AND '.join(where)} ORDER BY {order}",
        params)


@route("GET", "/api/tenders")
def search_tenders(req):
    user = current_user(req, required=False)
    profile = get_profile(user["id"]) if user else {}
    rows = _search_rows(req)

    eligible_only = req.arg("eligible_only") == "1"
    rank_by_profit = req.arg("sort") == "expected_profit"
    cost_ratio = req.arg("cost_ratio", 0.82, float)

    out = []
    for r in rows:
        t = tender_public(r)
        if profile:
            t["match"] = eligibility.match(profile, t["eligibility"], t)
            if eligible_only and t["match"]["verdict"] == "not_eligible":
                continue
        if rank_by_profit:
            t["score"] = pricing.quick_score(
                t["buyer"], t["category"], t["estimated_value"], cost_ratio)
        out.append(t)

    if rank_by_profit:
        out.sort(key=lambda t: -((t.get("score") or {}).get("expected_profit") or 0))

    page = req.arg("page", 1, int)
    size = min(req.arg("page_size", 20, int), 100)
    start = (page - 1) * size
    return {
        "total": len(out),
        "page": page,
        "page_size": size,
        "results": out[start:start + size],
    }


@route("GET", "/api/tenders/<int:tender_id>")
def tender_detail(req, tender_id):
    row = db.query_one("SELECT * FROM tenders WHERE id=?", (tender_id,))
    if not row:
        raise HttpError(404, "Tender not found")
    t = tender_public(row)

    user = current_user(req, required=False)
    if user:
        profile = get_profile(user["id"])
        t["match"] = eligibility.match(profile, t["eligibility"], t)
        t["checklist"] = eligibility.document_checklist(t["eligibility"], profile)
        t["pipeline"] = db.query_one(
            "SELECT * FROM pipeline WHERE user_id=? AND tender_id=?",
            (user["id"], tender_id))

    # Comparable history from the same bucket, newest first.
    t["comparables"] = db.query("""
        SELECT ref_no,title,awarded_at,estimated_value,winning_bid,n_bidders,winner,
               winning_bid*1.0/estimated_value AS l1_ratio
        FROM awards WHERE buyer=? AND category=?
        ORDER BY awarded_at DESC LIMIT 8""", (t["buyer"], t["category"]))
    return t


@route("POST", "/api/parse")
def parse_clause(req):
    """Paste any tender's eligibility clause block and see what we extract."""
    text = req.require("text")
    parsed = eligibility.parse_eligibility(text)
    user = current_user(req, required=False)
    out = {"parsed": parsed}
    if user:
        out["match"] = eligibility.match(get_profile(user["id"]), parsed)
    return out


# ------------------------------------------------------------------ pricing

def _tender_or_fields(req):
    tid = req.json("tender_id", cast=int)
    if tid:
        row = db.query_one("SELECT * FROM tenders WHERE id=?", (tid,))
        if not row:
            raise HttpError(404, "Tender not found")
        return row["buyer"], row["category"], row["estimated_value"], row
    return (req.require("buyer"), req.require("category"),
            float(req.require("estimated_value")), None)


@route("POST", "/api/pricing/recommend")
def pricing_recommend(req):
    buyer, category, value, row = _tender_or_fields(req)
    cost = req.json("cost", cast=float)
    if cost is None:
        # Fall back to a cost ratio if the user has not costed the job yet.
        cost = value * req.json("cost_ratio", 0.82, float)
    target = req.json("target_margin_pct", 8.0, float)
    result = pricing.recommend(buyer, category, value, cost, target)
    if row:
        result["tender"]["id"] = row["id"]
        result["tender"]["title"] = row["title"]
        result["tender"]["emd"] = row["emd"]
        result["tender"]["closes_at"] = row["closes_at"]
    return result


@route("GET", "/api/bucket/ratios")
def bucket_ratios(req):
    """
    The actual sample the pricing model fitted, so the histogram on screen is
    the real distribution rather than a reconstruction of it. Being able to
    look at your own training data is the difference between a model and an
    oracle.
    """
    buyer = req.arg("buyer")
    category = req.arg("category")
    value = req.arg("value", 0, float)
    model = pricing.get_model(buyer, category, value or 1)
    return {
        "bucket": model.bucket,
        "prior": model.prior_label,
        "ratios": model.ratios,
        "prior_ratios": model.prior_ratios[:1500],
        "l1_ratios": model.l1_ratios,
        "n_tenders": model.n_tenders,
        "shrinkage_weight": model.G.weight,
    }


@route("POST", "/api/pricing/evaluate")
def pricing_evaluate(req):
    buyer, category, value, _row = _tender_or_fields(req)
    cost = req.json("cost", cast=float) or value * 0.82
    bid = float(req.require("bid"))
    return pricing.evaluate_bid(buyer, category, value, cost, bid)


# ----------------------------------------------------------------- pipeline

@route("GET", "/api/pipeline")
def list_pipeline(req):
    user = current_user(req)
    rows = db.query("""
        SELECT p.*, t.title, t.buyer, t.category, t.estimated_value, t.emd,
               t.closes_at, t.ref_no
        FROM pipeline p JOIN tenders t ON t.id = p.tender_id
        WHERE p.user_id=? ORDER BY t.closes_at""", (user["id"],))
    for r in rows:
        r["days_left"] = days_left(r["closes_at"])
        if r.get("our_bid") and r.get("cost_est"):
            r["margin"] = r["our_bid"] - r["cost_est"]
    return {"pipeline": rows}


@route("POST", "/api/pipeline")
def upsert_pipeline(req):
    user = current_user(req)
    tid = int(req.require("tender_id"))
    status = req.json("status", "watching")
    if status not in ("watching", "preparing", "submitted", "won", "lost", "dropped"):
        raise HttpError(400, "Unknown status")
    if not db.query_one("SELECT id FROM tenders WHERE id=?", (tid,)):
        raise HttpError(404, "Tender not found")

    existing = db.query_one(
        "SELECT * FROM pipeline WHERE user_id=? AND tender_id=?", (user["id"], tid))
    fields = {
        "status": status,
        "our_bid": req.json("our_bid", existing["our_bid"] if existing else None, float),
        "cost_est": req.json("cost_est", existing["cost_est"] if existing else None, float),
        "emd_paid": req.json("emd_paid", existing["emd_paid"] if existing else 0, float) or 0,
        "notes": req.json("notes", existing["notes"] if existing else ""),
        "updated_at": TODAY.isoformat(),
    }
    if existing:
        db.execute("""UPDATE pipeline SET status=?,our_bid=?,cost_est=?,emd_paid=?,
                      notes=?,updated_at=? WHERE id=?""",
                   (fields["status"], fields["our_bid"], fields["cost_est"],
                    fields["emd_paid"], fields["notes"], fields["updated_at"],
                    existing["id"]))
        pid = existing["id"]
    else:
        pid = db.execute("""INSERT INTO pipeline
            (user_id,tender_id,status,our_bid,cost_est,emd_paid,notes,updated_at)
            VALUES (?,?,?,?,?,?,?,?)""",
                         (user["id"], tid, fields["status"], fields["our_bid"],
                          fields["cost_est"], fields["emd_paid"],
                          fields["notes"], fields["updated_at"]))
    return {"id": pid, **fields}


@route("DELETE", "/api/pipeline/<int:pid>")
def delete_pipeline(req, pid):
    user = current_user(req)
    db.execute("DELETE FROM pipeline WHERE id=? AND user_id=?", (pid, user["id"]))
    return {"deleted": pid}


# ---------------------------------------------------------------- portfolio

@route("POST", "/api/portfolio/optimise")
def optimise_portfolio(req):
    user = current_user(req)
    profile = get_profile(user["id"])
    capital = req.json("capital", cast=float)
    if capital is None:
        capital = (profile.get("working_capital_cr") or 0.25) * 10_000_000
    max_bids = req.json("max_bids", 6, int)
    cost_ratio = req.json("cost_ratio", 0.82, float)
    horizon = req.json("horizon_days", 45, int)
    eligible_only = bool(req.json("eligible_only", True))

    rows = db.query("""
        SELECT * FROM tenders
        WHERE julianday(closes_at) >= julianday(?)
          AND julianday(closes_at) - julianday(?) <= ?
        ORDER BY closes_at""", (TODAY.isoformat(), TODAY.isoformat(), horizon))

    candidates = []
    for r in rows:
        t = tender_public(r)
        if eligible_only and profile:
            m = eligibility.match(profile, t["eligibility"], t)
            if m["verdict"] == "not_eligible":
                continue
        score = pricing.quick_score(t["buyer"], t["category"],
                                    t["estimated_value"], cost_ratio)
        if not score or score["expected_profit"] <= 0:
            continue
        candidates.append({
            "id": t["id"], "title": t["title"], "buyer": t["buyer"],
            "closes_at": t["closes_at"], "emd": t["emd"],
            "estimated_value": t["estimated_value"],
            "recommended_bid": score["bid"], "win_prob": score["win_prob"],
            "expected_profit": score["expected_profit"],
        })

    # Keep the DP tractable: the best 40 by profit density is more than enough.
    candidates.sort(
        key=lambda c: -(c["expected_profit"] / c["emd"] if c["emd"] else 1e18))
    result = portfolio.optimise(candidates[:40], capital, max_bids)
    result["considered"] = len(candidates)
    return result


# -------------------------------------------------------------------- intel

@route("GET", "/api/competitors")
def competitors(req):
    where, params = ["1=1"], []
    if req.arg("buyer"):
        where.append("a.buyer=?")
        params.append(req.arg("buyer"))
    if req.arg("category"):
        where.append("a.category=?")
        params.append(req.arg("category"))
    rows = db.query(f"""
        SELECT b.bidder,
               COUNT(*)                                      AS bids,
               SUM(CASE WHEN b.rank=1 THEN 1 ELSE 0 END)     AS wins,
               AVG(b.amount*1.0/a.estimated_value)           AS avg_ratio,
               MIN(b.amount*1.0/a.estimated_value)           AS min_ratio,
               SUM(CASE WHEN b.rank=1 THEN a.estimated_value ELSE 0 END) AS won_value
        FROM bids b JOIN awards a ON a.id=b.award_id
        WHERE {' AND '.join(where)}
        GROUP BY b.bidder HAVING bids >= 3
        ORDER BY wins DESC, bids DESC LIMIT 25""", params)
    for r in rows:
        r["hit_rate"] = r["wins"] / r["bids"] if r["bids"] else 0
    return {"competitors": rows}


@route("GET", "/api/competitors/<str:name>")
def competitor_detail(req, name):
    rows = db.query("""
        SELECT a.ref_no,a.title,a.buyer,a.category,a.awarded_at,
               a.estimated_value,a.n_bidders,a.winner,
               b.amount,b.rank, b.amount*1.0/a.estimated_value AS ratio
        FROM bids b JOIN awards a ON a.id=b.award_id
        WHERE b.bidder=? ORDER BY a.awarded_at DESC LIMIT 60""", (name,))
    if not rows:
        raise HttpError(404, "No bidding history for that firm")
    wins = [r for r in rows if r["rank"] == 1]
    ratios = sorted(r["ratio"] for r in rows)
    from .engine import stats as st
    by_cat = {}
    for r in rows:
        c = by_cat.setdefault(r["category"], {"bids": 0, "wins": 0})
        c["bids"] += 1
        c["wins"] += 1 if r["rank"] == 1 else 0
    return {
        "bidder": name,
        "bids": len(rows),
        "wins": len(wins),
        "hit_rate": len(wins) / len(rows),
        "median_ratio": st.quantile(ratios, 0.5),
        "p10_ratio": st.quantile(ratios, 0.10),
        "categories": [{"category": k, **v} for k, v in sorted(
            by_cat.items(), key=lambda kv: -kv[1]["bids"])],
        "history": rows[:25],
    }


@route("GET", "/api/screens")
def screens(req):
    return cartel.run_screens(req.arg("buyer"), req.arg("category"))


@route("GET", "/api/screens/ranked")
def screens_ranked(req):
    return {"buckets": cartel.rank_buckets(req.arg("limit", 12, int))}


# ---------------------------------------------------------------- dashboard

@route("GET", "/api/dashboard")
def dashboard(req):
    user = current_user(req)
    profile = get_profile(user["id"])
    cost_ratio = req.arg("cost_ratio", 0.82, float)

    pipe = db.query("""
        SELECT p.*, t.title,t.buyer,t.category,t.estimated_value,t.emd,t.closes_at
        FROM pipeline p JOIN tenders t ON t.id=p.tender_id
        WHERE p.user_id=?""", (user["id"],))

    emd_locked = sum(p["emd_paid"] or 0 for p in pipe
                     if p["status"] in ("submitted",))
    capital = (profile.get("working_capital_cr") or 0) * 10_000_000

    live_ev = 0.0
    for p in pipe:
        if p["status"] != "submitted" or not p["our_bid"]:
            continue
        ev = pricing.evaluate_bid(p["buyer"], p["category"],
                                  p["estimated_value"],
                                  p["cost_est"] or p["estimated_value"] * cost_ratio,
                                  p["our_bid"])
        live_ev += ev["expected_profit"]

    decided = [p for p in pipe if p["status"] in ("won", "lost")]
    win_rate = (sum(1 for p in decided if p["status"] == "won") / len(decided)
                if decided else None)

    # Ranked opportunities: what to work on, sorted by expected profit.
    rows = db.query("""
        SELECT * FROM tenders
        WHERE julianday(closes_at) >= julianday(?)
        ORDER BY closes_at LIMIT 120""", (TODAY.isoformat(),))
    opportunities, screened_out = [], 0
    for r in rows:
        t = tender_public(r)
        m = eligibility.match(profile, t["eligibility"], t)
        if m["verdict"] == "not_eligible":
            screened_out += 1
            continue
        score = pricing.quick_score(t["buyer"], t["category"],
                                    t["estimated_value"], cost_ratio)
        if not score:
            continue
        opportunities.append({
            "id": t["id"], "title": t["title"], "buyer": t["buyer"],
            "category": t["category"], "estimated_value": t["estimated_value"],
            "emd": t["emd"], "closes_at": t["closes_at"],
            "days_left": t["days_left"], "match": m["verdict"],
            "match_score": m["score"], "gaps": len(m["blockers"]),
            **score,
        })
    opportunities.sort(key=lambda o: -o["expected_profit"])

    return {
        "company": user["company_name"],
        "metrics": {
            "emd_locked": emd_locked,
            "working_capital": capital,
            "capital_utilisation": (emd_locked / capital) if capital else 0,
            "expected_value_live": live_ev,
            "win_rate": win_rate,
            "decided_count": len(decided),
            "active_bids": sum(1 for p in pipe if p["status"] in
                               ("preparing", "submitted")),
            "screened_out": screened_out,
            "hours_saved": round(screened_out * 1.3),
        },
        "opportunities": opportunities[:15],
        "closing_soon": sorted(
            [p for p in pipe if p["status"] in ("preparing", "watching")],
            key=lambda p: p["closes_at"])[:5],
        "cost_ratio_assumption": cost_ratio,
    }


# ------------------------------------------------------------------- alerts

@route("GET", "/api/alerts")
def list_alerts(req):
    user = current_user(req)
    rows = db.query("""SELECT * FROM alerts WHERE user_id=?
                       ORDER BY read ASC, created_at DESC LIMIT 50""",
                    (user["id"],))
    return {"alerts": rows,
            "unread": sum(1 for r in rows if not r["read"])}


@route("POST", "/api/alerts/<int:aid>/read")
def read_alert(req, aid):
    user = current_user(req)
    db.execute("UPDATE alerts SET read=1 WHERE id=? AND user_id=?",
               (aid, user["id"]))
    return {"ok": True}


# ------------------------------------------------------------------- export

@route("GET", "/api/export/tenders")
def export_tenders(req):
    """
    Returns CSV as a string. The browser turns it into a download; keeping it
    in the JSON envelope means the API stays uniform.
    """
    rows = _search_rows(req)
    cols = ["ref_no", "title", "buyer", "buyer_state", "category", "portal",
            "estimated_value", "emd", "published_at", "closes_at"]
    lines = [",".join(cols)]
    for r in rows:
        lines.append(",".join(_csv(r[c]) for c in cols))
    return {"filename": "tenders.csv", "csv": "\n".join(lines),
            "rows": len(rows)}


def _csv(v):
    s = "" if v is None else str(v)
    return '"' + s.replace('"', '""') + '"' if any(c in s for c in ',"\n') else s
