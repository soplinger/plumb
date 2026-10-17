"""Tests for plumb. Run with `python3 -m pytest` or `python3 -m unittest discover -s tests`."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import plumb  # noqa: E402

EXAMPLE = os.path.join(ROOT, "examples", "bore.txt")


def D(text):
    return Decimal(text)


def run_cli(argv):
    """Run the CLI and return (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = plumb.main(argv)
    return code, out.getvalue(), err.getvalue()


def rules_for(values, sigma=1, center=0):
    lines = list(range(1, len(values) + 1))
    limits = plumb.Limits(D(str(center)), D(str(sigma)))
    return plumb.find_violations([D(str(v)) for v in values], lines, limits)


class ParseTests(unittest.TestCase):
    def test_parses_directives_readings_and_notes(self):
        data = plumb.parse_text("@name Bore\n@unit mm\n@target 10\n10.1 lot A\n9.9\n")
        self.assertEqual(data.name, "Bore")
        self.assertEqual(data.unit, "mm")
        self.assertEqual(data.target, D("10"))
        self.assertEqual([r.value for r in data.readings], [D("10.1"), D("9.9")])
        self.assertEqual([r.note for r in data.readings], ["lot A", ""])
        self.assertEqual([r.line for r in data.readings], [4, 5])

    def test_comments_blank_lines_and_crlf_are_ignored(self):
        data = plumb.parse_text("# header\r\n\r\n1\r\n2\r\n")
        self.assertEqual([r.line for r in data.readings], [3, 4])

    def test_glued_unit_is_an_error_with_its_line_number(self):
        with self.assertRaises(plumb.ParseError) as ctx:
            plumb.parse_text("10\n500g\n11\n")
        self.assertEqual(ctx.exception.errors, [(2, "'500g' is not a number")])

    def test_unit_in_note_position_is_a_note_not_a_value(self):
        data = plumb.parse_text("10 500g batch\n11\n")
        self.assertEqual(data.readings[0].value, D("10"))
        self.assertEqual(data.readings[0].note, "500g batch")

    def test_all_errors_are_collected_at_once(self):
        with self.assertRaises(plumb.ParseError) as ctx:
            plumb.parse_text("@bogus 1\n@decimals 3\n@decimals 4\nabc\n1\n")
        lines = [line for line, _ in ctx.exception.errors]
        self.assertEqual(lines, [1, 3, 4])

    def test_nan_and_infinity_are_rejected(self):
        with self.assertRaises(plumb.ParseError) as ctx:
            plumb.parse_text("NaN\nInfinity\n1\n")
        self.assertEqual([line for line, _ in ctx.exception.errors], [1, 2])

    def test_needs_at_least_two_readings(self):
        with self.assertRaises(plumb.ParseError) as ctx:
            plumb.parse_text("5\n")
        self.assertIn("need at least 2 readings", ctx.exception.errors[0][1])

    def test_lsl_must_be_below_usl(self):
        with self.assertRaises(plumb.ParseError):
            plumb.parse_text("@lsl 5\n@usl 4\n1\n2\n")

    def test_target_outside_spec_is_a_warning(self):
        data = plumb.parse_text("@target 20\n@lsl 9\n@usl 11\n10\n10\n")
        self.assertEqual(data.warnings, ["target is above the upper spec limit"])


class StatisticsTests(unittest.TestCase):
    def test_sample_standard_deviation_is_exact_root(self):
        sd = plumb.stdev([D("1"), D("2"), D("3"), D("4")])
        self.assertAlmostEqual(float(sd), (5 / 3) ** 0.5, places=12)

    def test_individuals_limits_use_average_moving_range(self):
        limits = plumb.control_limits([D("10"), D("11"), D("10"), D("11")])
        self.assertEqual(limits.center, D("10.5"))
        self.assertAlmostEqual(float(limits.sigma), 1 / 1.128, places=12)
        self.assertAlmostEqual(float(limits.at(3)), 10.5 + 3 / 1.128, places=12)

    def test_median_for_odd_and_even_counts(self):
        self.assertEqual(plumb.median([D("3"), D("1"), D("2")]), D("2"))
        self.assertEqual(plumb.median([D("4"), D("1"), D("2"), D("3")]), D("2.5"))

    def test_group_stats_groups_by_note_in_order(self):
        data = plumb.parse_text("1 A\n2 B\n3 A\n4\n")
        stats = plumb.group_stats(data)
        self.assertEqual(list(stats), ["A", "B", "(no note)"])
        self.assertEqual(stats["A"]["n"], 2)
        self.assertEqual(stats["A"]["mean"], D("2"))
        self.assertIsNone(stats["B"]["sd"])


class SignalRuleTests(unittest.TestCase):
    def test_one_three_sigma_rule(self):
        found = rules_for([0, 0, 0, 3.5])
        self.assertEqual([(v["line"], v["rule"]) for v in found], [(4, "1-3s")])

    def test_two_two_sigma_rule_same_side(self):
        found = rules_for([0, 2.5, 2.6])
        self.assertEqual([(v["line"], v["rule"]) for v in found], [(3, "2-2s")])

    def test_r_four_sigma_rule_across_centre(self):
        found = rules_for([0, 2.5, -2.5])
        self.assertEqual([(v["line"], v["rule"]) for v in found], [(3, "R-4s")])

    def test_four_one_sigma_rule_fires_once_on_fourth_point(self):
        found = rules_for([1.5, 1.5, 1.5, 1.5, 1.5])
        self.assertEqual([(v["line"], v["rule"]) for v in found], [(4, "4-1s")])

    def test_ten_x_rule_fires_once_on_tenth_point(self):
        found = rules_for([0.5] * 11)
        self.assertEqual([(v["line"], v["rule"]) for v in found], [(10, "10-x")])

    def test_seven_point_trend_fires_on_seventh_point(self):
        found = rules_for([1, 2, 3, 4, 5, 6, 7], sigma=100)
        self.assertEqual([(v["line"], v["rule"]) for v in found], [(7, "trend-7")])

    def test_zero_sigma_skips_all_rules(self):
        lines = [1, 2, 3]
        values = [D("1"), D("2"), D("3")]
        self.assertEqual(plumb.find_violations(values, lines, plumb.Limits(D("2"), D("0"))), [])


class CapabilityAndBiasTests(unittest.TestCase):
    def test_cp_and_cpk_for_centred_process(self):
        # Moving ranges are all 0.1, so sigma_within = 0.1 / 1.128.
        # Cp = 0.6 / (6 * sigma) = 1.128 exactly, and Cpk equals Cp when centred.
        cap = plumb.capability([D("9.9"), D("10.0"), D("10.1")], D("9.7"), D("10.3"))
        self.assertAlmostEqual(float(cap["cp"]), 1.128, places=6)
        self.assertAlmostEqual(float(cap["cpk"]), 1.128, places=6)

    def test_capability_is_none_without_spec_limits(self):
        self.assertIsNone(plumb.capability([D("1"), D("2")], None, None))

    def test_bias_reports_difference_and_percent(self):
        b = plumb.bias([D("10.5"), D("10.5"), D("10.5")], D("10"), D("0"))
        self.assertEqual(b["diff"], D("0.5"))
        self.assertEqual(b["pct"], D("5"))
        self.assertIsNone(b["t"])


class AnalysisTests(unittest.TestCase):
    def test_baseline_must_fit_the_readings(self):
        data = plumb.parse_text("1\n2\n3\n")
        with self.assertRaises(plumb.AnalysisError):
            plumb.analyze(data, baseline=5)
        with self.assertRaises(plumb.AnalysisError):
            plumb.analyze(data, baseline=1)

    def test_limits_come_only_from_baseline(self):
        data = plumb.parse_text("10\n10.1\n9.9\n50\n")
        res = plumb.analyze(data, baseline=3)
        self.assertEqual(res["baseline_count"], 3)
        self.assertLess(res["limits"]["ucl"], D("50"))
        self.assertTrue(any(v["line"] == 4 and v["rule"] == "1-3s" for v in res["violations"]))

    def test_example_file_shows_signals(self):
        res = plumb.analyze(plumb.load_file(EXAMPLE))
        rules = {v["rule"] for v in res["violations"]}
        self.assertFalse(res["in_control"])
        self.assertIn("10-x", rules)

    def test_render_chart_marks_signals(self):
        res = plumb.analyze(plumb.load_file(EXAMPLE))
        chart = plumb.render_chart(res, height=10, width=30)
        self.assertIn("X", chart)
        self.assertIn("lines", chart)

    def test_add_reading_appends_and_reports_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "log.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("10.0 first")
            line_no = plumb.add_reading(path, "10.2", "second")
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        self.assertEqual(line_no, 2)
        self.assertEqual(text, "10.0 first\n10.2 second\n")

    def test_add_reading_rejects_non_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                plumb.add_reading(os.path.join(tmp, "log.txt"), "12mm")


class CliTests(unittest.TestCase):
    def test_check_exits_one_when_signals_found(self):
        code, out, _ = run_cli(["check", EXAMPLE])
        self.assertEqual(code, 1)
        self.assertIn("Verdict: OUT OF CONTROL", out)

    def test_check_exits_zero_when_in_control(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "steady.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("10.00\n10.01\n9.99\n10.00\n10.01\n9.99\n")
            code, out, _ = run_cli(["check", path])
        self.assertEqual(code, 0)
        self.assertIn("Verdict: IN CONTROL", out)

    def test_json_output_is_valid(self):
        code, out, _ = run_cli(["check", EXAMPLE, "--json"])
        payload = json.loads(out)
        self.assertEqual(payload["count"], 24)
        self.assertEqual(code, 1)

    def test_lint_returns_two_and_reports_line_for_bad_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bad.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("10\n500g\n")
            code, _, err = run_cli(["lint", path])
        self.assertEqual(code, 2)
        self.assertIn("bad.txt:2: '500g' is not a number", err)

    def test_missing_file_returns_two(self):
        code, _, err = run_cli(["check", os.path.join(ROOT, "no-such-file.txt")])
        self.assertEqual(code, 2)
        self.assertIn("plumb:", err)


if __name__ == "__main__":
    unittest.main()
