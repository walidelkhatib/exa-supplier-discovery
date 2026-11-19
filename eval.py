"""Run each query in queries.txt in two Exa search types and write eval_results.csv.

Modes compared: "auto" vs "deep".
  - auto: Exa's default; a single search, then structured extraction. Fastest and
    cheapest type that supports output_schema.
  - deep: runs several searches per request. Exa recommends it when a schema's
    fields live on different pages (location, capabilities and certifications
    often do), at the cost of latency and price.
  "lite" isn't used because it rejects output_schema and system_prompt, so it
  can't run the same pipeline the app ships.

Both modes use the exact request the app makes (supplier_search.search_suppliers),
so the comparison reflects what a user would actually get.

Usage:  python eval.py            # all queries
        python eval.py --limit 2  # first 2 queries (smoke test)
"""

import argparse
import csv
import os
import sys

from supplier_search import FIELDS, TOP_N, get_client, search_suppliers

MODES = ["auto", "deep"]
OUT = "eval_results.csv"
COLUMNS = [
    "query", "mode", "latency_s", "api_search_time_s", "cost_usd", "cost_source",
    "n_suppliers", "fields_verified", "top5_urls", "suppliers", "error",
    "relevant_in_top5",  # fill in by hand: how many of the top 5 are real, matching suppliers
]


def load_queries(path="queries.txt"):
    with open(path) as f:
        return [l.strip() for l in f if l.strip() and not l.startswith("#")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--force", action="store_true", help="overwrite a hand-scored CSV")
    args = ap.parse_args()

    if os.path.exists(args.out) and not args.force:
        with open(args.out) as f:
            if any(r.get("relevant_in_top5", "").strip() for r in csv.DictReader(f)):
                sys.exit(f"{args.out} already has hand scores. Use --out other.csv or --force.")

    queries = load_queries()[: args.limit]
    exa = get_client()
    rows = []
    for q in queries:
        for mode in MODES:
            run = search_suppliers(q, mode=mode, exa=exa)
            verified = sum(s.fields[f].verified for s in run.suppliers for f in FIELDS)
            rows.append({
                "query": q,
                "mode": mode,
                "latency_s": run.latency_s,
                "api_search_time_s": run.api_search_time_s,
                "cost_usd": run.cost_usd,
                "cost_source": run.cost_source,
                "n_suppliers": len(run.suppliers),
                "fields_verified": f"{verified}/{len(run.suppliers) * len(FIELDS)}",
                # Top 5 = the suppliers shown to the user (what you score), falling back to raw result URLs.
                "top5_urls": " | ".join(
                    [s.website for s in run.suppliers][:TOP_N] or run.result_urls[:TOP_N]
                ),
                "suppliers": " | ".join(s.name for s in run.suppliers),
                "error": run.error or "",
                "relevant_in_top5": "",
            })
            print(f"[{mode:>4}] {run.latency_s:>6}s  ${run.cost_usd}  "
                  f"{len(run.suppliers)} suppliers  {q[:60]}")

    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    print(f"\nWrote {len(rows)} rows to {args.out}. Fill in relevant_in_top5, then run summarize.py.")


if __name__ == "__main__":
    main()
