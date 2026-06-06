#!/usr/bin/env python3
"""Read normalize-audio.py --measure-only JSON output and surface outliers.

Targets EBU R128 broadcast (-23 LUFS), but flags relative to whatever band
the user passes. Sorts by absolute deviation; bands fixed by --band so the
proposal stays opinionated."""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path

TARGET = -23.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("json_path")
    ap.add_argument("--band", type=float, default=3.0,
                    help="LU window — within ±band is 'OK', outside is flagged")
    ap.add_argument("--top", type=int, default=40,
                    help="how many worst offenders per direction to print")
    args = ap.parse_args()

    data = json.loads(Path(args.json_path).read_text())
    ok = [r for r in data if r.get("status") == "OK"]
    fail = [r for r in data if r.get("status") not in ("OK",)]

    too_loud, too_quiet, in_band, peak_risk = [], [], [], []
    for r in ok:
        i = r["input_i"]
        tp = r["input_tp"]
        delta = i - TARGET           # positive = louder than target
        r["delta"] = delta
        if tp > -1.0:
            peak_risk.append(r)
        if delta > args.band:
            too_loud.append(r)
        elif delta < -args.band:
            too_quiet.append(r)
        else:
            in_band.append(r)

    too_loud.sort(key=lambda r: -r["delta"])
    too_quiet.sort(key=lambda r: r["delta"])
    peak_risk.sort(key=lambda r: -r["input_tp"])

    def fmt(r):
        name = os.path.basename(r["path"])
        return f"  {r['delta']:+6.1f} LU  TP={r['input_tp']:+6.1f}  LRA={r['input_lra']:5.1f}  {name}"

    print(f"# Loudness report — target {TARGET:+.0f} LUFS, band ±{args.band:.1f} LU")
    print(f"# Total scanned: {len(ok)} OK, {len(fail)} failures")
    print(f"# In band:   {len(in_band):4d}")
    print(f"# Too loud:  {len(too_loud):4d}  (>{TARGET + args.band:+.1f} LUFS)")
    print(f"# Too quiet: {len(too_quiet):4d}  (<{TARGET - args.band:+.1f} LUFS)")
    print(f"# TP > -1 dBFS (clipping risk): {len(peak_risk)}")
    print()
    print(f"## Worst {min(args.top, len(too_loud))} TOO LOUD")
    for r in too_loud[: args.top]:
        print(fmt(r))
    print()
    print(f"## Worst {min(args.top, len(too_quiet))} TOO QUIET")
    for r in too_quiet[: args.top]:
        print(fmt(r))
    if peak_risk:
        print()
        print("## TP > -1 dBFS (clipping risk)")
        for r in peak_risk[: args.top]:
            print(fmt(r))
    if fail:
        print()
        print(f"## {len(fail)} files failed measurement")
        for r in fail[:20]:
            print(f"  {r.get('status')}  {r.get('detail','')}  {r.get('path','')}")


if __name__ == "__main__":
    main()
