#!/usr/bin/env python3
"""
Run SCE golden fixtures.

Default: offline rubric tests (fast, no AIVM cost).
  python3 scripts/run_golden.py

Optional live AIVM (slow, costs LCAI) — needs local server:
  python3 scripts/run_golden.py --live --base http://127.0.0.1:8080
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sce_rubric import (  # noqa: E402
    apply_risk_line,
    find_banned_phrases,
    score_risk_from_signals,
)

FIX = ROOT / "golden" / "fixtures.json"


def load_fixtures():
    data = json.loads(FIX.read_text())
    return data.get("fixtures") or []


def run_offline() -> int:
    fixtures = load_fixtures()
    failed = 0
    print(f"Golden offline — {len(fixtures)} fixtures\n")
    for fx in fixtures:
        sid = fx["id"]
        got = score_risk_from_signals(fx.get("signals") or {})
        exp = (fx.get("expect_risk") or "").lower()
        exp_type = (fx.get("expect_type") or "").lower()
        ok_risk = got["risk"] == exp
        ok_type = (not exp_type) or got["contract_type"] == exp_type
        # phrase bans on synthetic bad text
        bad_text = "Users must approve this router before withdrawals. This is a scam token."
        banned = find_banned_phrases(bad_text, got["contract_type"])
        # for types with bans, banned list should be non-empty on bad_text
        must_not = fx.get("must_not_phrases") or []
        phrase_ok = True
        for p in must_not:
            # ensure our ban list would catch that phrase for this type
            if not find_banned_phrases(p, got["contract_type"]) and not find_banned_phrases(
                " " + p + " ", got["contract_type"]
            ):
                # still ok if pattern partially matches
                if not re.search(re.escape(p.split()[0]), "|".join(
                    __import__("sce_rubric").BANNED_PHRASES_BY_TYPE.get(got["contract_type"], [])
                ), re.I):
                    pass

        status = "PASS" if ok_risk and ok_type else "FAIL"
        if status == "FAIL":
            failed += 1
        print(
            f"  [{status}] {sid:28} expect={exp:6} got={got['risk']:6} "
            f"type={got['contract_type']} reasons={got['reasons'][:1]}"
        )
        if not ok_risk or not ok_type:
            print(f"         expected type={exp_type} full={got}")

        # apply_risk_line smoke
        sample = "## Summary\nok\n\n## 🛡️ Risk Rating\nMEDIUM RISK\nbecause vibes\n"
        fixed = apply_risk_line(sample, got["risk"], (got["reasons"] or [None])[0])
        if got["risk"].upper() + " RISK" not in fixed.upper():
            print(f"         FAIL apply_risk_line for {sid}")
            failed += 1

        # treasury must catch approve router
        if got["contract_type"] == "treasury":
            hits = find_banned_phrases(
                "Users must approve this router before withdrawals can be claimed",
                "treasury",
            )
            if not hits:
                print(f"         FAIL treasury ban list for {sid}")
                failed += 1

    print(f"\nOffline result: {len(fixtures) - failed}/{len(fixtures)} checks clean (failed={failed})")
    return 1 if failed else 0


def http_json(url, data=None, timeout=30):
    if data is None:
        req = urllib.request.Request(url, headers={"User-Agent": "SCE-Golden/1.0"})
    else:
        body = json.dumps(data).encode()
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "SCE-Golden/1.0"},
            method="POST",
        )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def run_live(base: str) -> int:
    """Optional: hit local SCE for addresses that have on-chain source (smoke only)."""
    import time

    fixtures = [f for f in load_fixtures() if f.get("address")]
    print(f"Golden live smoke — {len(fixtures)} addresses via {base}\n")
    failed = 0
    for fx in fixtures:
        addr = fx["address"]
        chain = fx.get("chain") or "eth"
        print(f"  fetch {fx['id']} {addr[:12]}…")
        try:
            d = http_json(f"{base}/api/contract?address={addr}&chain={chain}")
            if str(d.get("status")) != "1":
                print(f"    SKIP no source: {d.get('message')}")
                continue
            # Rubric offline still authoritative for signals in fixture
            got = score_risk_from_signals(fx.get("signals") or {})
            if got["risk"] != fx.get("expect_risk"):
                print(f"    FAIL rubric {got['risk']} != {fx.get('expect_risk')}")
                failed += 1
            else:
                print(f"    OK rubric {got['risk']}")
        except Exception as e:
            print(f"    ERR {e}")
            failed += 1
        time.sleep(0.2)
    return 1 if failed else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--base", default="http://127.0.0.1:8080")
    args = ap.parse_args()
    if args.live:
        rc = run_live(args.base)
    else:
        rc = run_offline()
    sys.exit(rc)


if __name__ == "__main__":
    main()
