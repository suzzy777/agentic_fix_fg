#!/usr/bin/env python3
import sys, os, json

def norm(s: str) -> str:
    s = (s or "").upper()
    if s == "SUCCESS": return "PASS"
    if s == "FAIL":    return "FAIL"
    return s

def main():
    if len(sys.argv) < 2:
        print("FAIL")
        return 0
    p = sys.argv[1]
    try:
        if not os.path.isfile(p):
            print("FAIL"); return 0
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        results = (d.get("results") or {})
        vals = [norm((v or {}).get("result","")) for v in results.values()]
        if vals and all(v == "PASS" for v in vals):
            print("PASS")
        else:
            print("FAIL")
    except Exception:
        print("FAIL")
    return 0

if __name__ == "__main__":
    sys.exit(main())

