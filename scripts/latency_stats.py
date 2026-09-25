"""Latency stats for Local Dictate from logs/latency.log.

Usage: python3 scripts/latency_stats.py [path-to-latency.log]

Reports release-to-pasted latency against the PRD budgets:
short clips (<=3s) target <=1.0s; long clips target <=2.5s, hard ceiling 4s.
"""
import math
import re
import statistics
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "logs/latency.log"
rows = [
    (float(m.group(1)), int(m.group(2)))
    for m in re.finditer(
        r"\[latency\] clip=([\d.]+)s release->pasted=(\d+)ms",
        open(path).read(),
    )
]


def pct(values, p):
    s = sorted(values)
    return s[max(0, math.ceil(p / 100 * len(s)) - 1)]


short = [lat for clip, lat in rows if clip <= 3.0]
long = [lat for clip, lat in rows if clip > 3.0]
print(f"samples: {len(rows)}")
print(
    f"short <=3s: n={len(short)} median={statistics.median(short):.0f}ms "
    f"p90={pct(short, 90)}ms over-1s-target={sum(v > 1000 for v in short)}/{len(short)}"
)
print(
    f"long  >3s: n={len(long)} median={statistics.median(long):.0f}ms "
    f"p90={pct(long, 90)}ms over-2.5s-target={sum(v > 2500 for v in long)}/{len(long)} "
    f"over-4s-ceiling={sum(v > 4000 for v in long)}/{len(long)} max={max(long)}ms"
)
