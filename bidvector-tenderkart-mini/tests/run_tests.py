#!/usr/bin/env python3
"""
Test suite. Run with:  python3 tests/run_tests.py

No pytest -- unittest is in the standard library and this project does not
install anything. Covers the parts where being wrong would be invisible: the
statistics, the auction maths, the knapsack constraints, and the clause parser.
"""

import os
import sys
import math
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.engine import stats, eligibility, portfolio          # noqa: E402
from backend.engine.eligibility import LAKH, CRORE                # noqa: E402


class TestStats(unittest.TestCase):

    def test_quantile_matches_linear_interpolation(self):
        xs = [1, 2, 3, 4]
        self.assertAlmostEqual(stats.quantile(xs, 0.0), 1)
        self.assertAlmostEqual(stats.quantile(xs, 1.0), 4)
        self.assertAlmostEqual(stats.quantile(xs, 0.5), 2.5)
        self.assertAlmostEqual(stats.quantile(xs, 0.25), 1.75)

    def test_ecdf_is_a_proper_cdf(self):
        e = stats.ECDF([0.8, 0.9, 1.0, 1.1])
        self.assertEqual(e.cdf(0.0), 0.0)
        self.assertEqual(e.cdf(2.0), 1.0)
        self.assertEqual(e.cdf(0.9), 0.5)
        # monotone non-decreasing
        prev = -1
        for i in range(0, 200):
            v = e.cdf(i / 100)
            self.assertGreaterEqual(v, prev)
            prev = v

    def test_shrinkage_weight_follows_the_formula(self):
        e = stats.ECDF([1] * 8, [2] * 100, k=8.0)
        self.assertAlmostEqual(e.weight, 8 / 16)          # n / (n + k)
        e2 = stats.ECDF([1] * 24, [2] * 100, k=8.0)
        self.assertAlmostEqual(e2.weight, 24 / 32)
        # More data -> the bucket's own evidence dominates.
        self.assertGreater(e2.weight, e.weight)

    def test_shrinkage_pulls_toward_the_prior(self):
        sample = [0.95] * 4                     # thin, high-priced bucket
        prior = [0.80] * 200                    # wide, cheaper prior
        thin = stats.ECDF(sample, prior, k=8.0)
        fat = stats.ECDF([0.95] * 60, prior, k=8.0)
        # At 0.85 the prior says "already exceeded", the bucket says "not yet".
        # The thin bucket must sit closer to the prior than the fat one does.
        self.assertGreater(thin.cdf(0.85), fat.cdf(0.85))

    def test_no_prior_means_no_shrinkage(self):
        e = stats.ECDF([1, 2, 3], [])
        self.assertEqual(e.weight, 1.0)
        self.assertEqual(e.cdf(2), 2 / 3)

    def test_kde_integrates_to_about_one(self):
        xs = [0.8, 0.85, 0.9, 0.95, 1.0, 0.88, 0.92]
        lo, hi, n = 0.0, 2.0, 2000
        step = (hi - lo) / n
        area = sum(stats.kde(xs, lo + i * step) * step for i in range(n))
        self.assertAlmostEqual(area, 1.0, places=2)

    def test_kde_is_non_negative(self):
        xs = [0.8, 0.9, 1.0]
        for i in range(0, 200):
            self.assertGreaterEqual(stats.kde(xs, i / 100), 0.0)

    def test_bootstrap_ci_brackets_the_point_estimate(self):
        sample = [0.80, 0.85, 0.88, 0.90, 0.91, 0.93, 0.95, 0.99]
        reps = stats.bootstrap(sample, stats.mean, reps=400, seed=3)
        lo, hi = stats.ci(reps, 0.90)
        m = stats.mean(sample)
        self.assertLess(lo, m)
        self.assertGreater(hi, m)

    def test_benford_first_digit(self):
        self.assertEqual(stats.benford_first_digit(91234), 9)
        self.assertEqual(stats.benford_first_digit(0.00345), 3)
        self.assertEqual(stats.benford_first_digit(1), 1)

    def test_last_digits(self):
        self.assertEqual(stats.last_digits(1230000, 4), 0)
        self.assertEqual(stats.last_digits(1234567, 3), 567)


class TestPricingMaths(unittest.TestCase):
    """
    The pricing model reads from the database, so these tests exercise the
    maths directly against a hand-built distribution where the right answer
    can be worked out on paper.
    """

    class FakeModel:
        """Minimal stand-in exposing the same win_prob / profit surface."""

        def __init__(self, ratios, rivals):
            self.G = stats.ECDF(ratios, [])
            self.rival_counts = {rivals: 1.0}
            self.value = 1_000_000.0

        def win_prob(self, b):
            s = stats.clamp(self.G.survival(b), 0.0, 1.0)
            return sum(p * s ** n for n, p in self.rival_counts.items())

        def profit(self, b, c):
            return (b - c) * self.value * self.win_prob(b)

    def setUp(self):
        self.ratios = [0.80 + 0.01 * i for i in range(21)]   # 0.80 .. 1.00

    def test_win_probability_is_monotone_decreasing(self):
        m = self.FakeModel(self.ratios, 3)
        prev = 2.0
        for i in range(70, 110):
            p = m.win_prob(i / 100)
            self.assertLessEqual(p, prev + 1e-12)
            prev = p

    def test_win_probability_bounds(self):
        m = self.FakeModel(self.ratios, 3)
        self.assertAlmostEqual(m.win_prob(0.0), 1.0)
        self.assertAlmostEqual(m.win_prob(2.0), 0.0)

    def test_more_rivals_lowers_win_probability(self):
        few = self.FakeModel(self.ratios, 2)
        many = self.FakeModel(self.ratios, 8)
        for b in (0.85, 0.90, 0.95):
            self.assertGreater(few.win_prob(b), many.win_prob(b))

    def test_independence_formula(self):
        """P(win) must equal survival^N exactly for a fixed rival count."""
        m = self.FakeModel(self.ratios, 4)
        b = 0.88
        self.assertAlmostEqual(m.win_prob(b), m.G.survival(b) ** 4)

    def test_profit_is_zero_at_cost_and_negative_below(self):
        m = self.FakeModel(self.ratios, 3)
        c = 0.85
        self.assertAlmostEqual(m.profit(c, c), 0.0)
        self.assertLess(m.profit(c - 0.02, c), 0.0)

    def test_optimum_lies_above_cost(self):
        m = self.FakeModel(self.ratios, 3)
        c = 0.84
        grid = [c + i * 0.002 for i in range(120)]
        best = max(grid, key=lambda b: m.profit(b, c))
        self.assertGreater(best, c)

    def test_higher_cost_pushes_the_optimal_bid_up(self):
        m = self.FakeModel(self.ratios, 3)

        def argmax(c):
            grid = [c + i * 0.002 for i in range(140)]
            return max(grid, key=lambda b: m.profit(b, c))

        self.assertGreater(argmax(0.90), argmax(0.82))

    def test_gpv_inversion_recovers_a_cost_below_the_bid(self):
        """c = b - (1-G(b)) / ((n-1) g(b)) must sit strictly below b."""
        xs = self.ratios
        G = stats.ECDF(xs, [])
        h = stats.silverman_bandwidth(xs)
        b, opponents = 0.90, 4
        g = stats.kde(xs, b, h)
        self.assertGreater(g, 0)
        c = b - (1 - G.cdf(b)) / (opponents * g)
        self.assertLess(c, b)


class TestPortfolio(unittest.TestCase):

    def test_respects_the_capital_constraint(self):
        items = [{"id": i, "title": f"t{i}", "emd": 300_000,
                  "expected_profit": 100_000} for i in range(10)]
        r = portfolio.optimise(items, 1_000_000, max_bids=10)
        self.assertLessEqual(r["totals"]["emd_committed"], 1_000_000)
        self.assertEqual(r["totals"]["bids"], 3)      # 3 x 300k fits, 4 does not

    def test_respects_the_bid_count_constraint(self):
        items = [{"id": i, "title": f"t{i}", "emd": 10_000,
                  "expected_profit": 50_000} for i in range(20)]
        r = portfolio.optimise(items, 10_000_000, max_bids=4)
        self.assertEqual(r["totals"]["bids"], 4)

    def test_finds_the_known_optimum(self):
        """
        Hand-checked instance. Capacity 5 units (Rs 50k), one item of weight 5
        worth 100, or two items of weight 2 and 3 worth 70 + 80 = 150.
        A greedy ratio sort takes the 2-unit item (35/unit) then the 3-unit
        (26.7/unit) and happens to agree here, so we also assert the value.
        """
        items = [
            {"id": 1, "title": "big", "emd": 50_000, "expected_profit": 100},
            {"id": 2, "title": "a", "emd": 20_000, "expected_profit": 70},
            {"id": 3, "title": "b", "emd": 30_000, "expected_profit": 80},
        ]
        r = portfolio.optimise(items, 50_000, max_bids=3)
        self.assertEqual(r["totals"]["expected_profit"], 150)
        self.assertEqual({s["id"] for s in r["selected"]}, {2, 3})

    def test_beats_or_matches_greedy_on_a_trap_instance(self):
        """
        Greedy by profit-per-rupee is fooled here: the densest item blocks the
        pair that actually fills the knapsack.
        """
        items = [
            {"id": 1, "title": "dense", "emd": 60_000, "expected_profit": 66},
            {"id": 2, "title": "x", "emd": 50_000, "expected_profit": 50},
            {"id": 3, "title": "y", "emd": 50_000, "expected_profit": 50},
        ]
        r = portfolio.optimise(items, 100_000, max_bids=3)
        self.assertEqual(r["totals"]["expected_profit"], 100)
        self.assertGreaterEqual(r["greedy_baseline"]["uplift_from_optimiser"], 0)

    def test_never_selects_a_loss_making_bid(self):
        items = [{"id": 1, "title": "bad", "emd": 10_000, "expected_profit": -5000},
                 {"id": 2, "title": "good", "emd": 10_000, "expected_profit": 5000}]
        r = portfolio.optimise(items, 1_000_000, max_bids=5)
        self.assertEqual([s["id"] for s in r["selected"]], [2])

    def test_shadow_price_is_non_negative(self):
        items = [{"id": i, "title": f"t{i}", "emd": 100_000 + i * 10_000,
                  "expected_profit": 40_000 + i * 3_000} for i in range(12)]
        r = portfolio.optimise(items, 400_000, max_bids=6)
        self.assertGreaterEqual(r["shadow_price"]["per_lakh"], 0)

    def test_zero_capital_returns_nothing(self):
        items = [{"id": 1, "title": "t", "emd": 10_000, "expected_profit": 100}]
        r = portfolio.optimise(items, 0, max_bids=3)
        self.assertEqual(r["selected"], [])


class TestEligibilityParser(unittest.TestCase):

    CLAUSE = """
    3.1 The bidder shall have an average annual turnover of Rs. 2.50 Crore
    during the last three financial years, certified by a Chartered Accountant.
    3.2 The bidder must possess minimum 5 years experience in execution of works
    of similar nature under any Government department.
    3.3 The bidder should have successfully completed one similar work of value
    not less than Rs. 90.00 Lakh during the last seven years.
    3.4 The bidder shall hold valid GST registration and PAN. ISO 9001:2015
    certification is mandatory. A valid EPF and ESI registration code number
    shall be produced.
    3.5 Only Class-I local suppliers as defined in the Public Procurement
    (Preference to Make in India) Order 2017 are eligible to participate.
    3.6 Micro and Small Enterprises registered under Udyam are exempt from
    submission of EMD on production of a valid certificate.
    3.7 Joint venture or consortium bids shall not be permitted.
    """

    def setUp(self):
        self.p = eligibility.parse_eligibility(self.CLAUSE)

    def test_extracts_turnover_in_crore(self):
        self.assertAlmostEqual(self.p["min_turnover"], 2.5 * CRORE)

    def test_extracts_experience(self):
        self.assertEqual(self.p["min_experience_years"], 5.0)

    def test_extracts_similar_work_in_lakh(self):
        self.assertAlmostEqual(self.p["min_similar_work"], 90 * LAKH)

    def test_extracts_similar_work_count(self):
        self.assertEqual(self.p["similar_work_count"], 1)

    def test_extracts_certifications(self):
        for c in ("GST", "PAN", "ISO 9001", "EPF", "ESI", "Udyam"):
            self.assertIn(c, self.p["certifications"])

    def test_extracts_local_content_class(self):
        self.assertEqual(self.p["bidder_class"], "Class I")

    def test_detects_mse_relaxation_across_intervening_words(self):
        self.assertTrue(self.p["msme_relaxation"])

    def test_detects_joint_venture_prohibition(self):
        self.assertFalse(self.p["joint_venture_allowed"])

    def test_handles_indian_comma_grouping(self):
        p = eligibility.parse_eligibility(
            "The bidder shall have an average annual turnover of Rs. 1,25,00,000.")
        self.assertAlmostEqual(p["min_turnover"], 12_500_000)

    def test_flags_unreadable_thresholds_instead_of_guessing(self):
        p = eligibility.parse_eligibility(
            "The bidder must demonstrate adequate turnover to the satisfaction "
            "of the Engineer-in-Charge.")
        self.assertIsNone(p["min_turnover"])
        self.assertTrue(p["unparsed"])

    def test_empty_text_does_not_crash(self):
        p = eligibility.parse_eligibility("")
        self.assertIsNone(p["min_turnover"])
        self.assertEqual(p["certifications"], [])


class TestEligibilityMatching(unittest.TestCase):

    PROFILE = {
        "turnover_cr": 6.4, "experience_years": 9, "max_similar_work_cr": 2.75,
        "certifications": ["GST", "PAN", "EPF", "ESI", "Udyam", "ISO 9001"],
        "bidder_class": "Class II", "msme_class": "Small",
        "states": ["Maharashtra"], "working_capital_cr": 0.95,
    }

    def test_passes_when_every_criterion_is_met(self):
        crit = {"min_turnover": 2 * CRORE, "min_experience_years": 5,
                "min_similar_work": 50 * LAKH, "certifications": ["GST"],
                "bidder_class": "Class II", "unparsed": []}
        m = eligibility.match(self.PROFILE, crit)
        self.assertEqual(m["verdict"], "eligible")
        self.assertEqual(m["passes"], m["total"])

    def test_blocks_on_insufficient_turnover(self):
        crit = {"min_turnover": 20 * CRORE, "certifications": [], "unparsed": []}
        m = eligibility.match(self.PROFILE, crit)
        self.assertEqual(m["verdict"], "not_eligible")
        self.assertIn("Average annual turnover", m["blockers"])

    def test_class_two_cannot_satisfy_class_one(self):
        crit = {"bidder_class": "Class I", "certifications": [], "unparsed": []}
        m = eligibility.match(self.PROFILE, crit)
        self.assertEqual(m["verdict"], "not_eligible")

    def test_class_one_satisfies_a_class_two_requirement(self):
        prof = dict(self.PROFILE, bidder_class="Class I")
        crit = {"bidder_class": "Class II", "certifications": [], "unparsed": []}
        m = eligibility.match(prof, crit)
        self.assertEqual(m["verdict"], "eligible")

    def test_mse_relaxation_lowers_the_turnover_bar(self):
        # 8 Cr required, we have 6.4 Cr -> fails without relaxation,
        # passes with it (threshold drops 25% to 6 Cr).
        strict = {"min_turnover": 8 * CRORE, "certifications": [], "unparsed": []}
        relaxed = dict(strict, msme_relaxation=True)
        self.assertEqual(eligibility.match(self.PROFILE, strict)["verdict"],
                         "not_eligible")
        self.assertEqual(eligibility.match(self.PROFILE, relaxed)["verdict"],
                         "eligible")

    def test_missing_soft_certification_is_not_a_blocker(self):
        crit = {"certifications": ["ISO 14001"], "unparsed": []}
        m = eligibility.match(self.PROFILE, crit)
        self.assertEqual(m["verdict"], "conditional")
        self.assertEqual(m["blockers"], [])

    def test_missing_hard_certification_blocks(self):
        prof = dict(self.PROFILE, certifications=["PAN"])
        crit = {"certifications": ["GST"], "unparsed": []}
        m = eligibility.match(prof, crit)
        self.assertEqual(m["verdict"], "not_eligible")

    def test_unparsed_clauses_surface_as_review_not_silent_pass(self):
        crit = {"certifications": [], "unparsed": ["turnover threshold unreadable"]}
        m = eligibility.match(self.PROFILE, crit)
        self.assertEqual(m["verdict"], "review")

    def test_emd_beyond_working_capital_is_flagged_but_not_blocking(self):
        crit = {"certifications": [], "unparsed": []}
        m = eligibility.match(self.PROFILE, crit,
                              {"buyer_state": "Maharashtra", "emd": 5 * CRORE})
        emd = next(c for c in m["checks"] if c["criterion"] == "EMD affordability")
        self.assertEqual(emd["status"], "fail")
        self.assertFalse(emd["blocking"])

    def test_checklist_is_generated_from_criteria(self):
        crit = {"min_turnover": CRORE, "min_similar_work": LAKH,
                "certifications": ["ISO 9001"], "bidder_class": "Class II",
                "unparsed": []}
        items = eligibility.document_checklist(crit, self.PROFILE)
        labels = [i["item"] for i in items]
        self.assertTrue(any("CA-certified turnover" in l for l in labels))
        self.assertTrue(any("completion certificates" in l.lower() for l in labels))
        self.assertTrue(any("Local content" in l for l in labels))


class TestAuth(unittest.TestCase):

    def test_password_round_trip(self):
        from backend.auth import hash_password, verify_password
        h = hash_password("correct horse battery")
        self.assertTrue(verify_password("correct horse battery", h))
        self.assertFalse(verify_password("wrong", h))

    def test_hash_is_salted(self):
        from backend.auth import hash_password
        self.assertNotEqual(hash_password("same"), hash_password("same"))

    def test_token_round_trip_and_tamper_rejection(self):
        from backend.auth import make_token, read_token
        t = make_token(7, "a@b.in")
        self.assertEqual(read_token(t)["uid"], 7)
        body, _, sig = t.partition(".")
        self.assertIsNone(read_token(body + ".deadbeef"))
        self.assertIsNone(read_token(None))
        self.assertIsNone(read_token("garbage"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
