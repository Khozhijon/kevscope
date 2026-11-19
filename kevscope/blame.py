"""'What was the CPU doing while this thread waited?'

For one thread, take every interval it spent waiting (Ready, or blocked on a
mutex, by default) and add up which other threads were Running during those
intervals on the CPU the waiting thread ran on next (the CPU it was waiting for).
A high-priority thread that sits Mutex-blocked while an unrelated
medium-priority thread runs is the signature of a priority inversion.
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from . import model as m
from .stats import fmt_ns
from .timeline import ThreadKey, Timeline

DEFAULT_STATES = ("Ready", "Mutex-blocked", "Muon-mutex-blocked")


def is_idle(tl: Timeline, key: ThreadKey) -> bool:
    return tl.thread_names.get(key, "").startswith("idle_cpu")


def find_threads(tl: Timeline, pattern: str) -> List[ThreadKey]:
    """Threads whose 'process: thread' label contains `pattern`, or 'PID:TID'."""
    parts = pattern.split(":")
    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
        key = (int(parts[0]), int(parts[1]))
        return [key] if key in tl.states else []
    exact = [k for k in tl.states if tl.thread_label(k) == pattern]
    if exact:
        return sorted(exact)
    low = pattern.lower()
    return sorted(k for k in tl.states
                  if low in ("%s: %s" % (tl.proc_label(k[0]), tl.thread_label(k))).lower())


@dataclass
class Blame:
    key: ThreadKey
    state: str
    waits: int = 0
    total: int = 0
    longest: int = 0
    longest_at: int = 0
    covered: int = 0                         # waiting time during which someone else ran
    ran: Dict[ThreadKey, int] = field(default_factory=dict)


def blame(tl: Timeline, key: ThreadKey, states: Sequence[str] = DEFAULT_STATES) -> List[Blame]:
    ivs = tl.states.get(key, [])
    # Running intervals of every other thread, per CPU, sorted by start.
    runs: Dict[int, List[Tuple[int, int, ThreadKey]]] = {}
    for other, oivs in tl.states.items():
        if other == key:
            continue
        for iv in oivs:
            if iv.state == m.RUNNING and iv.dur > 0:
                runs.setdefault(iv.cpu, []).append((iv.start, iv.end, other))
    starts: Dict[int, List[int]] = {}
    longest_run: Dict[int, int] = {}
    for cpu, lst in runs.items():
        lst.sort()
        starts[cpu] = [r[0] for r in lst]
        longest_run[cpu] = max(r[1] - r[0] for r in lst)

    # The CPU a wait is charged to is the one the thread ran on next (it was
    # waiting for that CPU), or the one it last ran on if it never ran again.
    next_cpu: List[Optional[int]] = [None] * len(ivs)
    cpu: Optional[int] = None
    for i in range(len(ivs) - 1, -1, -1):
        if ivs[i].state == m.RUNNING:
            cpu = ivs[i].cpu
        next_cpu[i] = cpu
    cpu = None
    for i, iv in enumerate(ivs):
        if iv.state == m.RUNNING:
            cpu = iv.cpu
        elif next_cpu[i] is None:
            next_cpu[i] = cpu

    out: Dict[str, Blame] = {}
    for idx, iv in enumerate(ivs):
        if iv.state not in states or iv.dur <= 0:
            continue
        b = out.setdefault(iv.state, Blame(key, iv.state))
        b.waits += 1
        b.total += iv.dur
        if iv.dur > b.longest:
            b.longest, b.longest_at = iv.dur, iv.start
        on = next_cpu[idx]
        if on is None or on not in runs:
            continue
        lst = runs[on]
        i = bisect_left(starts[on], iv.start - longest_run[on])
        while i < len(lst) and lst[i][0] < iv.end:
            s, e, other = lst[i]
            overlap = min(e, iv.end) - max(s, iv.start)
            if overlap > 0:
                b.ran[other] = b.ran.get(other, 0) + overlap
                if not is_idle(tl, other):
                    b.covered += overlap
            i += 1
    return [out[s] for s in states if s in out]


def report(tl: Timeline, key: ThreadKey, states: Sequence[str] = DEFAULT_STATES,
           top: int = 8) -> str:
    who = "%s: %s (pid %d tid %d)" % (tl.proc_label(key[0]), tl.thread_label(key), key[0], key[1])
    results = blame(tl, key, states)
    if not results:
        return "%s was never in %s during this trace." % (who, " / ".join(states))
    lines: List[str] = [who]
    for b in results:
        lines.append("")
        lines.append("  %s: %d time%s, %s in total, longest %s at t=%s"
                     % (b.state, b.waits, "" if b.waits == 1 else "s", fmt_ns(b.total),
                        fmt_ns(b.longest), fmt_ns(b.longest_at)))
        ranked = sorted(b.ran.items(), key=lambda kv: kv[1], reverse=True)
        if not ranked:
            lines.append("  nothing else ran on the CPU it was waiting for (not traced, or the "
                         "wait was a hand-over between events)")
            continue
        lines.append("  meanwhile the CPU it was waiting for ran:")
        for other, ns in ranked[:top]:
            label = "%s: %s" % (tl.proc_label(other[0]), tl.thread_label(other))
            if is_idle(tl, other):
                label += "  (idle)"
            lines.append("    %-44s %10s  %5.1f%%" % (label[:44], fmt_ns(ns), 100.0 * ns / b.total))
        if len(ranked) > top:
            lines.append("    ... and %d more" % (len(ranked) - top))
    return "\n".join(lines)


def pick(tl: Timeline, pattern: str) -> Tuple[Optional[ThreadKey], str]:
    """Resolve a --thread pattern to one thread, or explain why not."""
    found = find_threads(tl, pattern)
    if len(found) == 1:
        return found[0], ""
    if not found:
        return None, "no thread matches %r" % pattern
    names = ", ".join("%s: %s (%d:%d)" % (tl.proc_label(k[0]), tl.thread_label(k), k[0], k[1])
                      for k in found[:8])
    return None, "%d threads match %r: %s%s" % (len(found), pattern, names,
                                               " ..." if len(found) > 8 else "")
