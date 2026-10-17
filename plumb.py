#!/usr/bin/env python3
"""Plumb: statistical process control for plain-text measurement logs.

Reads a plain-text file of measurements, builds individuals (I-MR) control
limits from a baseline, flags Westgard-style signals, and reports process
capability and bias against a target. Standard library only, no network.
"""

import argparse
import json
import sys
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

# d2 constant for a moving range of two consecutive readings.
D2 = Decimal("1.128")
DEFAULT_DECIMALS = 4
NUMERIC_DIRECTIVES = ("target", "lsl", "usl")
DIRECTIVES = ("name", "unit", "decimals", "baseline") + NUMERIC_DIRECTIVES
ZERO = Decimal(0)


class ParseError(Exception):
    """Raised with every problem found in a file, as (line or None, message)."""

    def __init__(self, errors):
        self.errors = errors
        super().__init__("; ".join(msg for _, msg in errors))


class AnalysisError(Exception):
    """Raised when a request cannot be analysed, such as a bad baseline."""


@dataclass
class Reading:
    line: int
    value: Decimal
    note: str = ""


@dataclass
class Dataset:
    name: str = "measurements"
    unit: str = ""
    target: Decimal = None
    lsl: Decimal = None
    usl: Decimal = None
    decimals: int = None
    baseline: int = None
    readings: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def _to_decimal(text):
    """Return a finite Decimal for text, or None if it is not a plain number."""
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _apply_directive(data, key, value):
    if key in ("name", "unit"):
        if not value:
            raise ValueError(f"'@{key}' needs a value")
        setattr(data, key, value)
    elif key in NUMERIC_DIRECTIVES:
        number = _to_decimal(value)
        if number is None:
            raise ValueError(f"'@{key}' needs a number, got '{value}'")
        setattr(data, key, number)
    elif key == "decimals":
        if not value.isdigit() or int(value) > 10:
            raise ValueError("'@decimals' needs a whole number from 0 to 10")
        data.decimals = int(value)
    elif key == "baseline":
        if not value.isdigit() or int(value) < 2:
            raise ValueError("'@baseline' needs a whole number of at least 2")
        data.baseline = int(value)


def parse_text(text):
    """Parse a measurement file. Collects every error before raising."""
    data = Dataset()
    errors = []
    seen = set()
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("@"):
            parts = line[1:].split(None, 1)
            key = parts[0].lower() if parts else ""
            value = parts[1].strip() if len(parts) > 1 else ""
            if key not in DIRECTIVES:
                errors.append((line_no, f"unknown directive '@{key}'"))
            elif key in seen:
                errors.append((line_no, f"duplicate directive '@{key}'"))
            else:
                seen.add(key)
                try:
                    _apply_directive(data, key, value)
                except ValueError as exc:
                    errors.append((line_no, str(exc)))
            continue
        tokens = line.split(None, 1)
        number = _to_decimal(tokens[0])
        if number is None:
            errors.append((line_no, f"'{tokens[0]}' is not a number"))
            continue
        note = tokens[1].strip() if len(tokens) > 1 else ""
        data.readings.append(Reading(line_no, number, note))

    if data.lsl is not None and data.usl is not None and data.lsl >= data.usl:
        errors.append((None, "'@lsl' must be below '@usl'"))
    if len(data.readings) < 2 and not errors:
        errors.append((None, f"need at least 2 readings, found {len(data.readings)}"))
    if errors:
        raise ParseError(errors)

    if data.target is not None:
        if data.lsl is not None and data.target < data.lsl:
            data.warnings.append("target is below the lower spec limit")
        if data.usl is not None and data.target > data.usl:
            data.warnings.append("target is above the upper spec limit")
    return data


def load_file(path):
    with open(path, encoding="utf-8-sig") as handle:
        return parse_text(handle.read())


def mean(values):
    return sum(values, ZERO) / len(values)


def stdev(values):
    """Sample standard deviation (n - 1), computed exactly before the root."""
    m = mean(values)
    variance = sum(((v - m) ** 2 for v in values), ZERO) / (len(values) - 1)
    return variance.sqrt()


def median(values):
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def moving_ranges(values):
    return [abs(b - a) for a, b in zip(values, values[1:])]


@dataclass
class Limits:
    center: Decimal
    sigma: Decimal

    def at(self, k):
        """The line k sigmas from the centre line (k may be negative)."""
        return self.center + k * self.sigma


def control_limits(values):
    """Individuals (I-MR) limits: sigma estimated from the average moving range."""
    return Limits(mean(values), mean(moving_ranges(values)) / D2)


def find_violations(values, lines, limits):
    """Apply Westgard-style signal rules plus a 7-point trend rule.

    Each run-based rule fires once, when its run first reaches its length,
    so one long run is reported once rather than on every extra point.
    """
    violations = []

    def add(line, rule, message):
        violations.append({"line": line, "rule": rule, "message": message})

    if limits.sigma == 0:
        return violations

    z_scores = [(v - limits.center) / limits.sigma for v in values]
    one_sigma_side, one_sigma_run = 0, 0
    centre_side, centre_run = 0, 0
    trend_dir, trend_run = 0, 0

    for i, (value, z, line) in enumerate(zip(values, z_scores, lines)):
        if abs(z) > 3:
            add(line, "1-3s", f"{value} is beyond 3 sigma (z={z:+.2f})")
        if i > 0:
            prev = z_scores[i - 1]
            if (z > 2 and prev > 2) or (z < -2 and prev < -2):
                add(line, "2-2s", "two consecutive readings beyond 2 sigma on the same side")
            if (z > 2 and prev < -2) or (z < -2 and prev > 2):
                add(line, "R-4s", "readings jump from beyond -2 sigma to beyond +2 sigma")

        side = 1 if z > 1 else -1 if z < -1 else 0
        if side and side == one_sigma_side:
            one_sigma_run += 1
        else:
            one_sigma_side, one_sigma_run = side, 1 if side else 0
        if one_sigma_run == 4:
            word = "above +1" if side > 0 else "below -1"
            add(line, "4-1s", f"4 consecutive readings {word} sigma")

        side = 1 if z > 0 else -1 if z < 0 else 0
        if side and side == centre_side:
            centre_run += 1
        else:
            centre_side, centre_run = side, 1 if side else 0
        if centre_run == 10:
            word = "above" if side > 0 else "below"
            add(line, "10-x", f"10 consecutive readings {word} the centre line")

        if i > 0:
            step = (value > values[i - 1]) - (value < values[i - 1])
            if step and step == trend_dir:
                trend_run += 1
            else:
                trend_dir, trend_run = step, 1 if step else 0
            if trend_run == 6:
                word = "rising" if step > 0 else "falling"
                add(line, "trend-7", f"7 consecutive readings {word}")
    return violations


def _index(m, sigma, lsl, usl):
    """Return (potential, actual) capability index for one sigma estimate."""
    if sigma == 0:
        return None, None
    potential = None
    if lsl is not None and usl is not None:
        potential = (usl - lsl) / (6 * sigma)
    sides = []
    if usl is not None:
        sides.append((usl - m) / (3 * sigma))
    if lsl is not None:
        sides.append((m - lsl) / (3 * sigma))
    return potential, min(sides) if sides else None


def capability(values, lsl, usl):
    if lsl is None and usl is None:
        return None
    m = mean(values)
    within_sigma = mean(moving_ranges(values)) / D2
    overall_sigma = stdev(values)
    cp, cpk = _index(m, within_sigma, lsl, usl)
    pp, ppk = _index(m, overall_sigma, lsl, usl)
    return {"cp": cp, "cpk": cpk, "pp": pp, "ppk": ppk}


def bias(values, target, sd):
    if target is None:
        return None
    m = mean(values)
    diff = m - target
    pct = diff / target * 100 if target != 0 else None
    t_stat = diff / (sd / Decimal(len(values)).sqrt()) if sd else None
    return {"diff": diff, "pct": pct, "t": t_stat}


def analyze(data, baseline=None):
    """Run the full analysis and return a plain dict (Decimals inside)."""
    readings = data.readings
    values = [r.value for r in readings]
    lines = [r.line for r in readings]
    n = len(values)
    base_n = baseline if baseline is not None else (data.baseline or n)
    if not 2 <= base_n <= n:
        raise AnalysisError(f"baseline must be between 2 and {n} readings, got {base_n}")

    limits = control_limits(values[:base_n])
    warnings = list(data.warnings)
    if limits.sigma == 0:
        warnings.append("baseline has no variation; limits are flat and signal rules were skipped")
    violations = find_violations(values, lines, limits)
    m = mean(values)
    sd = stdev(values)
    out_of_spec = sum(
        1 for v in values
        if (data.lsl is not None and v < data.lsl) or (data.usl is not None and v > data.usl)
    )
    return {
        "name": data.name,
        "unit": data.unit,
        "decimals": data.decimals if data.decimals is not None else DEFAULT_DECIMALS,
        "count": n,
        "baseline_count": base_n,
        "first_line": lines[0],
        "last_line": lines[-1],
        "mean": m,
        "sd": sd,
        "min": min(values),
        "max": max(values),
        "median": median(values),
        "limits": {
            "center": limits.center,
            "sigma": limits.sigma,
            "ucl": limits.at(3), "lcl": limits.at(-3),
            "uwl": limits.at(2), "lwl": limits.at(-2),
            "uzl": limits.at(1), "lzl": limits.at(-1),
        },
        "violations": violations,
        "in_control": not violations,
        "capability": capability(values, data.lsl, data.usl),
        "lsl": data.lsl,
        "usl": data.usl,
        "out_of_spec": out_of_spec,
        "target": data.target,
        "bias": bias(values, data.target, sd),
        "warnings": warnings,
        "points": [{"line": r.line, "value": r.value, "note": r.note} for r in readings],
    }


def group_stats(data):
    """Per-note summary, in order of first appearance. Blank notes are '(no note)'."""
    groups = {}
    for reading in data.readings:
        groups.setdefault(reading.note or "(no note)", []).append(reading.value)
    stats = {}
    for label, values in groups.items():
        stats[label] = {
            "n": len(values),
            "mean": mean(values),
            "sd": stdev(values) if len(values) > 1 else None,
            "min": min(values),
            "max": max(values),
        }
    return stats


def fmt(x, places):
    quantum = Decimal(1).scaleb(-places)
    return f"{x.quantize(quantum, rounding=ROUND_HALF_UP):f}"


def _rate_cpk(cpk):
    if cpk is None:
        return ""
    if cpk >= Decimal("1.33"):
        return "capable"
    if cpk >= 1:
        return "marginal"
    return "not capable"


def format_report(res):
    d = res["decimals"]
    unit = f" {res['unit']}" if res["unit"] else ""

    def show(x, suffix=""):
        return "n/a" if x is None else f"{fmt(x, d)}{suffix}"

    lim = res["limits"]
    out = [
        f"Plumb report: {res['name']}",
        f"Readings: {res['count']} (lines {res['first_line']}-{res['last_line']}),"
        f" limits from first {res['baseline_count']}",
        "",
        "Summary",
        f"  mean      {show(res['mean'], unit)}",
        f"  sd        {show(res['sd'], unit)}",
        f"  median    {show(res['median'], unit)}",
        f"  min..max  {show(res['min'])} .. {show(res['max'], unit)}",
        "",
        "Control limits (I-MR)",
        f"  UCL {show(lim['ucl'])}   3 sigma",
        f"  UWL {show(lim['uwl'])}   2 sigma",
        f"  UZL {show(lim['uzl'])}   1 sigma",
        f"  CL  {show(lim['center'])}",
        f"  LZL {show(lim['lzl'])}",
        f"  LWL {show(lim['lwl'])}",
        f"  LCL {show(lim['lcl'])}",
        "",
    ]
    if res["violations"]:
        out.append("Signals")
        for v in res["violations"]:
            out.append(f"  line {v['line']:<5} {v['rule']:<8} {v['message']}")
    else:
        out.append("Signals: none")
    out.append("")

    if res["capability"]:
        cap = res["capability"]
        out.append("Capability")
        out.append(f"  Cp  {show(cap['cp'])}   Pp  {show(cap['pp'])}")
        out.append(f"  Cpk {show(cap['cpk'])}   Ppk {show(cap['ppk'])}   {_rate_cpk(cap['cpk'])}")
        out.append(f"  out of spec: {res['out_of_spec']} of {res['count']}")
        out.append("")

    if res["bias"]:
        b = res["bias"]
        verdict = ""
        if b["t"] is not None:
            verdict = "biased (|t| >= 2)" if abs(b["t"]) >= 2 else "no clear bias"
        pct = "" if b["pct"] is None else f" ({'+' if b['pct'] >= 0 else ''}{fmt(b['pct'], 2)}%)"
        out.append(f"Bias vs target {show(res['target'])}")
        out.append(f"  mean - target {'+' if b['diff'] >= 0 else ''}{fmt(b['diff'], d)}{pct}")
        if b["t"] is not None:
            out.append(f"  t = {fmt(b['t'], 2)}: {verdict}")
        out.append("")

    if res["warnings"]:
        out.append("Warnings")
        out.extend(f"  - {w}" for w in res["warnings"])
        out.append("")

    if res["in_control"]:
        out.append("Verdict: IN CONTROL")
    else:
        count = len(res["violations"])
        out.append(f"Verdict: OUT OF CONTROL ({count} signal{'s' if count != 1 else ''})")
    return "\n".join(out)


def render_chart(res, height=12, width=70):
    """Text control chart of the most recent `width` readings."""
    points = res["points"][-width:]
    lim = res["limits"]
    d = res["decimals"]
    flagged = {v["line"] for v in res["violations"]}
    top = max([lim["ucl"]] + [p["value"] for p in points])
    bottom = min([lim["lcl"]] + [p["value"] for p in points])
    if top == bottom:
        return "(all readings are identical; nothing to chart)"
    span = top - bottom

    def row(value):
        r = ((top - value) / span * (height - 1)).to_integral_value(rounding=ROUND_HALF_UP)
        return min(max(int(r), 0), height - 1)

    grid = [[" "] * len(points) for _ in range(height)]
    for level, mark in ((lim["ucl"], "="), (lim["lcl"], "="), (lim["center"], "-")):
        for col in range(len(points)):
            cell = grid[row(level)]
            if cell[col] == " ":
                cell[col] = mark
    for col, p in enumerate(points):
        grid[row(p["value"])][col] = "X" if p["line"] in flagged else "o"

    labels = [fmt(top - span * r / (height - 1), d) for r in range(height)]
    width_label = max(len(s) for s in labels)
    out = [f"{labels[r]:>{width_label}} |{''.join(grid[r])}" for r in range(height)]
    out.append(" " * width_label + " +" + "-" * len(points))
    out.append(
        f"lines {points[0]['line']}..{points[-1]['line']}"
        "   o = reading, X = signal, = = limits, - = centre"
    )
    return "\n".join(out)


def add_reading(path, value_text, note=""):
    """Append one reading to the file. Returns the line number it was written to."""
    if _to_decimal(value_text) is None:
        raise ValueError(f"'{value_text}' is not a number")
    if "\n" in note or "\r" in note:
        raise ValueError("a note must fit on one line")
    try:
        with open(path, encoding="utf-8") as handle:
            existing = handle.read()
    except FileNotFoundError:
        existing = ""
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    line = value_text + (f" {note}" if note else "")
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(prefix + line + "\n")
    return len((existing + prefix + line).splitlines())


def _json_default(obj):
    if isinstance(obj, Decimal):
        return str(obj)
    raise TypeError(f"not serialisable: {type(obj).__name__}")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="plumb",
        description="Statistical process control for plain-text measurement logs.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="full report: limits, signals, capability, bias")
    check.add_argument("file")
    check.add_argument("--baseline", type=int, help="readings used to set the limits")
    check.add_argument("--json", action="store_true", help="print machine-readable JSON")

    chart = sub.add_parser("chart", help="text control chart of the readings")
    chart.add_argument("file")
    chart.add_argument("--baseline", type=int, help="readings used to set the limits")
    chart.add_argument("--width", type=int, default=70, help="most recent readings to show")

    groups = sub.add_parser("groups", help="summary per note, such as a lot or shift tag")
    groups.add_argument("file")

    lint = sub.add_parser("lint", help="check the file for errors and warnings only")
    lint.add_argument("file")

    add = sub.add_parser("add", help="append one reading to the file")
    add.add_argument("file")
    add.add_argument("value")
    add.add_argument("--note", default="", help="optional tag, such as a lot number")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.command == "add":
            line_no = add_reading(args.file, args.value, args.note)
            print(f"Added reading {args.value} at line {line_no}")
            return 0

        data = load_file(args.file)
        if args.command == "lint":
            print(f"OK: {len(data.readings)} readings")
            for warning in data.warnings:
                print(f"warning: {warning}")
            return 0

        if args.command == "groups":
            stats = group_stats(data)
            places = data.decimals if data.decimals is not None else DEFAULT_DECIMALS
            print(f"{'group':<20}{'n':>4}{'mean':>12}{'sd':>12}{'min':>12}{'max':>12}")
            for label, s in stats.items():
                sd = "n/a" if s["sd"] is None else fmt(s["sd"], places)
                print(
                    f"{label:<20}{s['n']:>4}{fmt(s['mean'], places):>12}{sd:>12}"
                    f"{fmt(s['min'], places):>12}{fmt(s['max'], places):>12}"
                )
            return 0

        res = analyze(data, args.baseline)
        if args.command == "chart":
            print(render_chart(res, width=args.width))
            return 0
        if args.json:
            print(json.dumps(res, indent=2, default=_json_default))
        else:
            print(format_report(res))
        return 0 if res["in_control"] else 1
    except ParseError as exc:
        for line_no, message in exc.errors:
            where = f"{args.file}:{line_no}" if line_no else args.file
            print(f"{where}: {message}", file=sys.stderr)
        return 2
    except (AnalysisError, OSError, ValueError) as exc:
        print(f"plumb: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
