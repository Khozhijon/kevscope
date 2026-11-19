"""Scheduling statistics computed from a Timeline."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from . import model as m
from .timeline import ThreadKey, Timeline


@dataclass
class ThreadStats:
    key: ThreadKey
    process: str
    thread: str
    run_ns: int            # total time in Running
    runs: int              # number of Running intervals
    wakeups: int           # Ready -> Running transitions measured
    lat_min: int           # Ready -> Running latency, nanoseconds
    lat_avg: int
    lat_p99: int
    lat_max: int
    lat_max_at: int        # timestamp of the worst case


def percentile(sorted_vals: List[int], pct: float) -> int:
    """Nearest-rank percentile of an ascending list."""
    if not sorted_vals:
        return 0
    rank = max(1, int(-(-pct * len(sorted_vals) // 100)))  # ceil
    return sorted_vals[min(rank, len(sorted_vals)) - 1]


def thread_stats(tl: Timeline) -> List[ThreadStats]:
    out: List[ThreadStats] = []
    for key, ivs in tl.states.items():
        run_ns = runs = 0
        lats: List[int] = []
        worst_at = 0
        worst = -1
        for i, iv in enumerate(ivs):
            if iv.state == m.RUNNING:
                run_ns += iv.dur
                runs += 1
            elif iv.state == m.READY and i + 1 < len(ivs) and ivs[i + 1].state == m.RUNNING:
                if iv.dur > worst:
                    worst, worst_at = iv.dur, iv.start
                lats.append(iv.dur)
        lats.sort()
        out.append(ThreadStats(
            key, tl.proc_label(key[0]), tl.thread_label(key), run_ns, runs, len(lats),
            lats[0] if lats else 0,
            sum(lats) // len(lats) if lats else 0,
            percentile(lats, 99), lats[-1] if lats else 0, worst_at))
    out.sort(key=lambda s: s.run_ns, reverse=True)
    return out


def cpu_busy(tl: Timeline) -> Dict[int, int]:
    """Nanoseconds each CPU spent running any thread other than idle (pid 1 on
    QNX is procnto, whose low tids are the per-CPU idle threads; they are
    counted as busy here because the trace alone does not mark them)."""
    busy: Dict[int, int] = {c: 0 for c in tl.cpus}
    for ivs in tl.states.values():
        for iv in ivs:
            if iv.state == m.RUNNING:
                busy[iv.cpu] = busy.get(iv.cpu, 0) + iv.dur
    return busy


def fmt_ns(ns: int) -> str:
    if ns >= 1_000_000_000:
        return "%.3f s" % (ns / 1e9)
    if ns >= 1_000_000:
        return "%.3f ms" % (ns / 1e6)
    if ns >= 1_000:
        return "%.1f us" % (ns / 1e3)
    return "%d ns" % ns


def report(tl: Timeline, top: int = 20, show_idle: bool = False) -> str:
    rows = [s for s in thread_stats(tl)
            if show_idle or not s.thread.startswith("idle_cpu")][:top]
    lines = ["Trace length %s, %d CPU(s), %d threads, %d message flows, %d interrupts"
             % (fmt_ns(tl.end_ts), len(tl.cpus), len(tl.states), len(tl.flows),
                len(tl.interrupts)),
             "",
             "%-22s %-18s %10s %6s %7s %10s %10s %10s %10s" % (
                 "process", "thread", "cpu time", "runs", "wakeups",
                 "lat min", "lat avg", "lat p99", "lat max")]
    for s in rows:
        lines.append("%-22s %-18s %10s %6d %7d %10s %10s %10s %10s" % (
            s.process[:22], s.thread[:18], fmt_ns(s.run_ns), s.runs, s.wakeups,
            fmt_ns(s.lat_min), fmt_ns(s.lat_avg), fmt_ns(s.lat_p99), fmt_ns(s.lat_max)))
    if tl.unmatched:
        lines.append("")
        lines.append("Events that could not be paired:")
        for k, v in sorted(tl.unmatched.items()):
            lines.append("  %s: %d" % (k, v))
    return "\n".join(lines)
