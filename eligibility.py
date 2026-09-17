"""
Eligibility parsing and matching.

Two jobs:

1.  parse_eligibility(text) reads the free-text "Eligibility Criteria" clause
    block that every Indian tender document carries and pulls out structured
    requirements -- turnover, experience, similar-work value, certifications,
    local-content class. This is deliberately rule-based (regex over the
    phrasing that actually recurs in CPPP/GeM documents) rather than an LLM
    call: it is inspectable, it costs nothing, it runs offline, and when it
    fails it fails visibly instead of hallucinating a turnover threshold.
    Anything it cannot parse is surfaced as "needs manual review" rather than
    silently dropped.

2.  match(profile, criteria) compares the parsed criteria against the bidder's
    company profile and returns a pass/fail per clause with the shortfall
    quantified and, where it is fixable, what to do about it.

The value of this is not the matching -- it is the screening. An MSME owner
reading 40 tender PDFs a week spends most of that time discovering they were
never eligible in the first place.
"""

import re
import json


LAKH = 100_000
CRORE = 10_000_000

# Certifications and registrations we know how to look for.
CERT_PATTERNS = [
    ("GST",             r"\bGST(?:IN)?\b|goods and services tax"),
    ("PAN",             r"\bPAN\b|permanent account number"),
    ("EPF",             r"\bEPF\b|\bPF\b registration|provident fund"),
    ("ESI",             r"\bESI(?:C)?\b|employees'? state insurance"),
    ("ISO 9001",        r"ISO\s*9001"),
    ("ISO 14001",       r"ISO\s*14001"),
    ("Udyam",           r"udyam|udyog aadhaar|\bMSME\b registration"),
    ("NSIC",            r"\bNSIC\b"),
    ("Electrical License", r"electrical contractor licen[cs]e|\bHT\b licen[cs]e"),
    ("PWD Registration",   r"(?:PWD|CPWD|state PWD)\s*(?:class\s*[A-Z]+\s*)?registration"),
    ("BIS",             r"\bBIS\b|bureau of indian standards"),
]


def _money(num_text, unit_text):
    """Convert '2.5' + 'crore' into rupees. Handles the Indian comma grouping."""
    n = float(num_text.replace(",", ""))
    u = (unit_text or "").lower()
    if "cr" in u:
        return n * CRORE
    if "lakh" in u or "lac" in u or u.strip() == "l":
        return n * LAKH
    return n


MONEY_RE = (
    r"(?:rs\.?|inr|₹)?\s*"
    r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*"
    r"(crore?s?|cr\.?|lakhs?|lacs?|l\b)?"
)


def parse_eligibility(text):
    """
    Extract structured criteria from a clause block.

    Returns a dict with the criteria found plus `unparsed`, the clauses we
    recognised as requirements but could not quantify. Those go in front of
    the user rather than being swallowed.
    """
    t = " ".join(text.split())
    low = t.lower()
    out = {
        "min_turnover": None,
        "min_experience_years": None,
        "min_similar_work": None,
        "similar_work_count": None,
        "certifications": [],
        "bidder_class": None,
        "msme_relaxation": False,
        "startup_relaxation": False,
        "joint_venture_allowed": None,
        "unparsed": [],
        "raw": text,
    }

    # --- average annual turnover -------------------------------------------
    m = re.search(
        r"(?:average\s+)?annual\s+turnover[^.;]{0,80}?" + MONEY_RE, low)
    if not m:
        m = re.search(r"turnover[^.;]{0,60}?not\s+less\s+than\s*" + MONEY_RE, low)
    if m:
        out["min_turnover"] = _money(m.group(1), m.group(2))
    elif "turnover" in low:
        out["unparsed"].append("A turnover requirement is mentioned but the "
                               "threshold could not be read automatically.")

    # --- experience ---------------------------------------------------------
    m = re.search(r"([0-9]+)\s*(?:\+)?\s*years?[^.;]{0,40}?"
                  r"(?:experience|standing|in\s+the\s+field)", low)
    if not m:
        m = re.search(r"experience[^.;]{0,40}?([0-9]+)\s*years?", low)
    if m:
        out["min_experience_years"] = float(m.group(1))

    # --- similar work -------------------------------------------------------
    m = re.search(
        r"similar\s+(?:nature\s+of\s+)?works?[^.;]{0,100}?"
        r"(?:value|costing|amounting|not\s+less\s+than)[^.;]{0,30}?" + MONEY_RE,
        low)
    if not m:
        m = re.search(r"(?:one|single|1)\s+similar\s+completed\s+work[^.;]{0,60}?"
                      + MONEY_RE, low)
    if m:
        out["min_similar_work"] = _money(m.group(1), m.group(2))
    elif "similar work" in low or "similar nature" in low:
        out["unparsed"].append("A similar-work requirement is mentioned but the "
                               "value threshold could not be read automatically.")

    m = re.search(r"(three|two|one|[0-9]+)\s+similar\s+(?:completed\s+)?works?", low)
    if m:
        words = {"one": 1, "two": 2, "three": 3}
        g = m.group(1)
        out["similar_work_count"] = words.get(g, int(g) if g.isdigit() else None)

    # --- certifications -----------------------------------------------------
    for name, pat in CERT_PATTERNS:
        if re.search(pat, low, re.I):
            out["certifications"].append(name)

    # --- local content class (Public Procurement Preference Order) ----------
    m = re.search(r"class[-\s]*(i{1,2}|1|2)\s*local\s*supplier", low)
    if m:
        roman = {"i": "Class I", "ii": "Class II", "1": "Class I", "2": "Class II"}
        out["bidder_class"] = roman.get(m.group(1), None)

    # The exemption sentence almost never puts the trigger word next to the
    # entity ("Micro and Small Enterprises registered under Udyam are exempt
    # from..."), so allow clause-internal distance but stop at the sentence end.
    out["msme_relaxation"] = bool(
        re.search(r"(msme|micro\s+and\s+small|\bmse\b)[^.;]{0,90}?"
                  r"(exempt|relax|waiv)", low))
    out["startup_relaxation"] = bool(
        re.search(r"start[-\s]?ups?[^.;]{0,90}?(exempt|relax|waiv)", low))
    if "joint venture" in low or "consortium" in low:
        out["joint_venture_allowed"] = not bool(
            re.search(r"(joint venture|consortium)[^.;]{0,40}not\s+(?:be\s+)?"
                      r"(allowed|permitted)", low))
    return out


# ------------------------------------------------------------------ matching

def _fmt(v):
    if v is None:
        return "-"
    if v >= CRORE:
        return f"Rs {v / CRORE:.2f} Cr"
    if v >= LAKH:
        return f"Rs {v / LAKH:.2f} L"
    return f"Rs {v:,.0f}"


def match(profile, criteria, tender=None):
    """
    Compare a company profile against parsed criteria.

    profile keys used: turnover_cr, experience_years, max_similar_work_cr,
    certifications (list), bidder_class, msme_class, states (list),
    working_capital_cr.
    """
    checks = []

    def add(name, ok, requirement, yours, note="", blocking=True, fixable=None):
        checks.append({
            "criterion": name,
            "status": "pass" if ok is True else ("fail" if ok is False else "review"),
            "requirement": requirement,
            "yours": yours,
            "note": note,
            "blocking": blocking,
            "remediation": fixable,
        })

    is_mse = (profile.get("msme_class") or "").lower() in ("micro", "small")

    # Turnover ---------------------------------------------------------------
    req_t = criteria.get("min_turnover")
    have_t = (profile.get("turnover_cr") or 0) * CRORE
    if req_t:
        relaxed = criteria.get("msme_relaxation") and is_mse
        threshold = req_t * (0.75 if relaxed else 1.0)
        ok = have_t >= threshold
        note = ("MSE relaxation applied (threshold reduced 25%)."
                if relaxed else "")
        add("Average annual turnover", ok, _fmt(threshold), _fmt(have_t), note,
            fixable=None if ok else
            "Turnover is a hard filter. Consider a joint venture with a larger "
            "partner if the tender permits one.")

    # Experience -------------------------------------------------------------
    req_e = criteria.get("min_experience_years")
    have_e = profile.get("experience_years") or 0
    if req_e:
        ok = have_e >= req_e
        add("Years in business", ok, f"{req_e:.0f} years", f"{have_e:.0f} years",
            fixable=None if ok else "Not fixable before this deadline.")

    # Similar work -----------------------------------------------------------
    req_s = criteria.get("min_similar_work")
    have_s = (profile.get("max_similar_work_cr") or 0) * CRORE
    if req_s:
        ok = have_s >= req_s
        shortfall = req_s - have_s
        add("Similar work executed", ok, _fmt(req_s), _fmt(have_s),
            "" if ok else f"Short by {_fmt(shortfall)} on your largest single work.",
            fixable=None if ok else
            "Check whether the buyer counts aggregated works or ongoing work "
            "certificates -- many departments do, and the clause rarely says so.")

    # Certifications ---------------------------------------------------------
    have_certs = {c.strip().lower() for c in (profile.get("certifications") or [])}
    for cert in criteria.get("certifications", []):
        ok = cert.lower() in have_certs
        add(f"{cert} registration", ok, "Required",
            "On file" if ok else "Not on file",
            blocking=cert not in ("ISO 14001", "BIS", "NSIC"),
            fixable=None if ok else _cert_fix(cert))

    # Local content class ----------------------------------------------------
    req_c = criteria.get("bidder_class")
    if req_c:
        have_c = profile.get("bidder_class") or "Non-local"
        rank = {"Class I": 2, "Class II": 1, "Non-local": 0}
        ok = rank.get(have_c, 0) >= rank.get(req_c, 0)
        add("Local content class", ok, req_c, have_c,
            "Public Procurement (Preference to Make in India) Order.",
            fixable=None if ok else
            "Class II needs >=20% local content, Class I >=50%, self-certified "
            "with a statutory auditor certificate above Rs 10 Cr.")

    # Geography --------------------------------------------------------------
    if tender:
        states = [s.lower() for s in (profile.get("states") or [])]
        bstate = (tender.get("buyer_state") or "").lower()
        if states and bstate and bstate not in states:
            add("Operating geography", None, tender.get("buyer_state"),
                ", ".join(profile.get("states") or []),
                "Outside your registered operating states -- check whether the "
                "buyer requires local registration or a site office.",
                blocking=False,
                fixable="Many state departments accept a declared site office. "
                        "Confirm before spending on the bid.")

        # EMD affordability is an eligibility question in practice.
        emd = tender.get("emd") or 0
        cap = (profile.get("working_capital_cr") or 0) * CRORE
        if emd and cap:
            ok = emd <= cap
            add("EMD affordability", ok, _fmt(emd), _fmt(cap) + " working capital",
                "" if ok else "EMD exceeds your stated working capital.",
                blocking=False,
                fixable=None if ok else
                "MSEs registered under Udyam are exempt from EMD for most "
                "central procurements -- claim the exemption instead of paying.")

    # Anything the parser flagged -------------------------------------------
    for u in criteria.get("unparsed", []):
        add("Manual review needed", None, "See clause", "-", u,
            blocking=False,
            fixable="Read the clause yourself -- the parser did not trust its "
                    "own reading of it.")

    passes = sum(1 for c in checks if c["status"] == "pass")
    fails = [c for c in checks if c["status"] == "fail"]
    blockers = [c for c in fails if c["blocking"]]
    reviews = [c for c in checks if c["status"] == "review"]
    total = len(checks) or 1

    if blockers:
        verdict = "not_eligible"
        headline = f"Blocked on {len(blockers)} criterion" + ("" if len(blockers) == 1 else "a")
    elif fails:
        verdict = "conditional"
        headline = f"{len(fails)} soft gap" + ("" if len(fails) == 1 else "s")
    elif reviews:
        verdict = "review"
        headline = "Eligible, with clauses needing a human read"
    else:
        verdict = "eligible"
        headline = "Meets every parsed criterion"

    return {
        "verdict": verdict,
        "headline": headline,
        "score": round(100 * passes / total),
        "passes": passes,
        "total": total,
        "blockers": [c["criterion"] for c in blockers],
        "checks": checks,
    }


def _cert_fix(cert):
    return {
        "GST": "Register on the GST portal; 3-7 working days.",
        "PAN": "Required for any bid. Apply immediately.",
        "EPF": "EPFO registration is same-week if you already have 20+ employees.",
        "ESI": "ESIC registration via Shram Suvidha; usually 2-3 days.",
        "ISO 9001": "Certification takes 4-8 weeks. Too slow for this tender, "
                    "worth starting for the next one.",
        "Udyam": "Udyam registration is free, online, and issues instantly. "
                 "It also unlocks EMD exemption and the 25% MSE set-aside.",
        "NSIC": "NSIC single-point registration takes 4-6 weeks.",
        "Electrical License": "State electrical licensing board; renewal is "
                              "faster than first issue.",
    }.get(cert, "Obtain before the bid submission deadline.")


def document_checklist(criteria, profile):
    """
    The paperwork pack for a bid. Mundane and the single most common reason a
    technically-capable MSME gets disqualified: a missing attested copy.
    """
    items = [
        ("Bid submission form (as per Annexure)", True),
        ("EMD instrument / MSE exemption certificate", True),
        ("PAN card copy", True),
        ("GST registration certificate", True),
        ("Audited balance sheets, last 3 financial years", True),
        ("CA-certified turnover statement", bool(criteria.get("min_turnover"))),
        ("Work completion certificates from client departments",
         bool(criteria.get("min_similar_work"))),
        ("Power of attorney for the signatory", True),
        ("Non-blacklisting affidavit on Rs 100 stamp paper", True),
        ("Local content self-declaration (Make in India)",
         bool(criteria.get("bidder_class"))),
        ("Udyam registration certificate", True),
        ("Digital signature certificate, Class 3", True),
    ]
    for cert in criteria.get("certifications", []):
        items.append((f"{cert} certificate copy", True))

    have = {c.lower() for c in (profile.get("certifications") or [])}
    out = []
    for label, required in items:
        if not required:
            continue
        known = any(h in label.lower() for h in have)
        out.append({"item": label, "on_file": known})
    return out
