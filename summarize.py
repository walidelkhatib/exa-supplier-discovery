"""Turn a hand-scored eval_results.csv into a markdown table for the README."""

import sys

import pandas as pd

path = sys.argv[1] if len(sys.argv) > 1 else "eval_results.csv"
try:
    df = pd.read_csv(path)
except FileNotFoundError:
    sys.exit(f"{path} not found. Run eval.py first.")
df["relevant_in_top5"] = pd.to_numeric(df["relevant_in_top5"], errors="coerce")

print("| Mode | Queries | Avg latency (s) | Avg cost (USD) | Avg suppliers returned | Fields verified | Avg relevant in top 5 |")
print("| --- | --- | --- | --- | --- | --- | --- |")
for mode, g in df.groupby("mode", sort=False):
    parts = g["fields_verified"].astype(str).str.split("/", expand=True).astype(int)
    verified = f"{parts[0].sum()}/{parts[1].sum()} ({parts[0].sum() / max(parts[1].sum(), 1):.0%})"
    cost = f"{g['cost_usd'].mean():.4f}"
    if (g["cost_source"] == "estimated").any():
        cost += " (estimated)"
    scored = g["relevant_in_top5"].dropna()
    rel = f"{scored.mean():.1f} (n={len(scored)})" if len(scored) else "not scored"
    print(f"| {mode} | {len(g)} | {g['latency_s'].mean():.1f} | {cost} | "
          f"{g['n_suppliers'].mean():.1f} | {verified} | {rel} |")
