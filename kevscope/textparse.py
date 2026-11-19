"""Decoder for the text that QNX `traceprinter` prints.

Written from the output format QNX documents publicly (System Analysis
Toolkit User's Guide and the traceprinter page of the Utilities Reference for
SDP 8.0): a header block of `NAME:: value` lines, then one line per event,

    t:0x4f86e756 CPU:00 COMM    :SND_MESSAGE   rcvid:0x0000004f pid:159762

with some events (process names) continuing on indented `key:value` lines.

Assumption to confirm against a real QNX 8.0 capture: how the 64-bit time is
rebuilt. The docs only say that `t:` shows the low 32 bits of the cycle
counter and that `CONTROL :TIME` events carry the high word (`msb`). This
decoder uses msb from the latest TIME event and also counts a rollover when
the low word drops by more than 2**31.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from .model import Event, Trace, Value

_EVENT = re.compile(r"^t:0x([0-9a-fA-F]+)\s+CPU:(\d+)\s+(.*)$")
_HEADER = re.compile(r"^\s*(TRACE_[A-Z0-9_]+)::\s?(.*)$")
_PAIR = re.compile(r'([A-Za-z_][\w>()\[\].-]*):("(?:[^"\\]|\\.)*"|\S+)')
_ASSIGN = re.compile(r"(\w+)\s*=\s*(\w+)")
_CONT = re.compile(r"^\s*([A-Za-z_][\w>()\[\].-]*):(.*)$")
_INT_CLASSES = ("INT_ENTR", "INT_EXIT", "INT_HANDLER_ENTR", "INT_HANDLER_EXIT")


def looks_like_text(data: bytes) -> bool:
    head = data[:65536]
    return (b"TRACEPRINTER" in head or b"-- KERNEL EVENTS --" in head
            or re.search(rb"(?m)^t:0x[0-9a-fA-F]+\s+CPU:\d+", head) is not None)


_HEX_DEC = re.compile(r"^(0x[0-9a-fA-F]+)\(\d+\)$")     # e.g. intr_id:0x1b(27)


def _value(text: str) -> Value:
    text = text.rstrip(",")
    hd = _HEX_DEC.match(text)
    if hd is not None:
        return int(hd.group(1), 16)
    try:
        if text[:2].lower() == "0x":
            return int(text, 16)
        return int(text, 10)
    except ValueError:
        return text.strip('"') if text[:1] == '"' else text


def _split(rest: str) -> Tuple[str, str, Dict[str, Value]]:
    """`CLASS :EVENT args...` -> (class, event, args)."""
    cls, sep, tail = rest.partition(":")
    cls = cls.strip()
    if not sep:
        return cls, "", {}
    tail = tail.strip()
    name, _, argtext = tail.partition(" ")
    args: Dict[str, Value] = {}
    if cls in ("KER_CALL", "KER_EXIT") and "/" in name:
        name, _, num = name.partition("/")
        args["number"] = _value(num)
    elif cls in _INT_CLASSES:
        try:
            args["interrupt"] = int(name, 0)
        except ValueError:
            args["interrupt"] = name
    for k, v in _PAIR.findall(argtext):
        args[k] = _value(v)
    if not args or cls == "CONTROL":
        for k, v in _ASSIGN.findall(argtext):   # "sequence = 33, num_events = 714"
            args.setdefault(k, _value(v))
    return cls, name, args


def parse(text: str) -> Trace:
    trace = Trace()
    raw: List[Tuple[int, Event]] = []      # (64-bit cycle count, event)
    last: Optional[Event] = None
    high = 0            # current high 32 bits of the cycle counter
    prev_low: Optional[int] = None
    seen_time = False
    skipped = 0

    for line in text.splitlines():
        mo = _EVENT.match(line)
        if mo is None:
            hm = _HEADER.match(line)
            if hm is not None and not raw:
                trace.header[hm.group(1)] = hm.group(2).strip()
                continue
            cm = _CONT.match(line)
            if cm is not None and last is not None:
                last.args[cm.group(1)] = _value(cm.group(2).strip())
            elif line.strip() and not line.lstrip().startswith(("--", "TRACEP", '"')):
                skipped += 1        # (a line starting with a quote is the tail of a
                                    # message dump that contained a newline)
            continue

        stamp = int(mo.group(1), 16)
        cls, name, args = _split(mo.group(3))
        if stamp > 0xFFFFFFFF:                 # already a full 64-bit count
            cycles = stamp
            high, prev_low = stamp >> 32, stamp & 0xFFFFFFFF
        else:
            if cls == "CONTROL" and name == "TIME" and isinstance(args.get("msb"), int):
                msb = int(args["msb"])
                if not seen_time and msb != high:
                    # Lines printed before the first TIME event (the BUFFER
                    # line) belong to the same high word.
                    shift = (msb - high) << 32
                    raw = [(c + shift, e) for c, e in raw]
                seen_time = True
                high = msb
            elif prev_low is not None and prev_low - stamp > 0x80000000:
                high += 1
            prev_low = stamp
            cycles = (high << 32) | stamp
        last = Event(0, int(mo.group(2)), cls, name, args)
        raw.append((cycles, last))

    if skipped:
        trace.warnings.append("%d line(s) not understood and skipped" % skipped)
    if not raw:
        return trace

    try:
        hz = int(trace.header.get("TRACE_CYCLES_PER_SEC", ""))
    except ValueError:
        hz = 0
    if hz <= 0:
        hz = 1_000_000_000
        trace.warnings.append("TRACE_CYCLES_PER_SEC missing: treating one cycle as one nanosecond")

    base = min(c for c, _ in raw)
    backwards = 0
    prev = base
    for cycles, ev in raw:
        if cycles < prev:
            backwards += 1
        prev = cycles
        ev.ts = (cycles - base) * 1_000_000_000 // hz
    if backwards:
        trace.warnings.append("%d event(s) had a timestamp earlier than the event before; "
                              "events were sorted by time" % backwards)
        raw.sort(key=lambda p: p[0])        # stable: equal stamps keep file order
    trace.events = [ev for _, ev in raw]
    return trace
