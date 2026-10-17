# Plumb

**Statistical process control for plain-text measurement logs.**

Plumb reads a plain-text file of measurements, sets control limits from a baseline run, flags out-of-control signals, and reports process capability and bias against a target. It runs entirely offline on the Python standard library, with no accounts, no servers, and no network calls.

## Features

- **Plain-text input** with `@directives` for name, unit, target, spec limits, decimals and baseline, and one reading per line with an optional note such as a lot or shift tag.
- **Line-numbered errors.** Every problem in a file is reported at once as `file:line: message`, so `500g` in a value column is caught rather than silently skipped.
- **Individuals (I-MR) control limits** with sigma estimated from the average moving range, plus the ±1, ±2 and ±3 sigma zones.
- **Baseline-frozen limits.** Set limits from the first N readings with `@baseline` or `--baseline`, then check every later reading against them.
- **Westgard-style signal rules:** 1-3s, 2-2s, R-4s, 4-1s and 10-x. Each run-based rule fires once, when the run reaches its length.
- **Trend detection.** A `trend-7` signal fires on seven consecutive rising or falling readings.
- **Process capability** with Cp, Cpk, Pp and Ppk, a plain-English rating for Cpk, and a count of readings out of spec.
- **Bias against target** showing the difference, the percentage and a rough t-statistic check.
- **Per-group summaries** with `plumb groups`, so you can compare lots or shifts side by side.
- **Text control chart** in the terminal, with signals marked as `X`.
- **Append a reading** from the command line with `plumb add`, which keeps the file in the right format.
- **Lint mode** for CI: `plumb lint` checks the file and exits non-zero on errors.
- **JSON output** with `--json` for scripts and dashboards.
- **Exit codes** that work in pipelines: `0` in control, `1` signals found, `2` input or usage error.

## Quick start

The project includes `examples/bore.txt`, a bore-diameter log for a turned bushing with a baseline of 12 readings and a drift in the second lot.

```bash
python3 plumb.py check examples/bore.txt
```

For the chart view, run:

```bash
python3 plumb.py chart examples/bore.txt
```

To add a new reading to the log:

```bash
python3 plumb.py add examples/bore.txt 10.021 --note "lot C"
```

## Input format

```text
# comments start with #
@name Bore diameter
@unit mm
@target 10.00
@lsl 9.95
@usl 10.05
@decimals 3
@baseline 12

10.012 lot A
9.996 lot A
10.004 lot B
```

- Directives are `@name`, `@unit`, `@target`, `@lsl`, `@usl`, `@decimals` (0 to 10) and `@baseline` (at least 2). Each may appear once.
- A reading is a number, optionally followed by a note. Blank lines and `#` comments are ignored, and CRLF line endings are accepted.
- Readings are analysed in file order, which is treated as time order.

## How to run

Requires Python 3 (developed on 3.12). There is nothing to install.

| Command | What it does |
| --- | --- |
| `python3 plumb.py check FILE [--baseline N] [--json]` | Full report: summary, limits, signals, capability and bias |
| `python3 plumb.py chart FILE [--baseline N] [--width W]` | Text control chart of the latest readings |
| `python3 plumb.py groups FILE` | Summary per note, such as a lot or shift |
| `python3 plumb.py lint FILE` | Check the file for errors and warnings only |
| `python3 plumb.py add FILE VALUE [--note TEXT]` | Append one reading |

Exit codes: `0` means in control, `1` means at least one signal was found, and `2` means an input or usage error.

## How to test

```bash
python3 -m pytest tests/
```

Pytest is optional. The suite is written with `unittest`, so this also works with no extra packages:

```bash
python3 -m unittest discover -s tests
```

The tests cover the parser and its line-numbered errors, the statistics, each signal rule on hand-built sequences, capability and bias, baseline handling, the chart, appending readings, and the CLI exit codes.

## Notes on the method

- Limits use the I-MR formulas with the d2 constant 1.128 for moving ranges of two readings.
- Cp and Cpk use the within-sigma estimate from moving ranges. Pp and Ppk use the overall sample standard deviation.
- Cpk of 1.33 or more is rated capable, 1.00 to 1.33 marginal, and below 1.00 not capable.
- The bias check flags |t| of 2 or more as a rough signal, not a formal hypothesis test.

## Project layout

```text
plumb.py            the tool (parser, analysis, report, CLI)
examples/bore.txt   sample measurement log
tests/test_plumb.py test suite
```
