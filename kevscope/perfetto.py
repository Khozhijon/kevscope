"""Export a Timeline as Trace Event Format JSON, which ui.perfetto.dev and
chrome://tracing both open.

Track layout:
  * a "CPUs" pseudo-process with, per CPU, one track of the thread running on
    it and one track of interrupt slices;
  * one process group per QNX process, one track per thread carrying its
    state intervals (Running, Ready, Receive-blocked, ...);
  * kernel calls on a second track right under the thread that made them
    (the state track is fully tiled, so calls cannot nest inside it);
  * message passing as flow arrows from the sending thread to the receiver,
    and from the replying thread back to the client.
"""
from __future__ import annotations

import json
from typing import IO, Dict, List

from . import model as m
from .timeline import ThreadKey, Timeline

CPU_PID = 0x7FFF0000      # pseudo-process id, far above any QNX pid we render
IRQ_TID_BASE = 1000
CALL_TID_BASE = 1_000_000    # pseudo thread ids for the per-thread kernel-call tracks


def _us(ns: int) -> float:
    return round(ns / 1000.0, 3)


def to_events(tl: Timeline, kercalls: bool = True) -> List[dict]:
    ev: List[dict] = []

    def meta(kind: str, pid: int, tid: int, **args) -> None:
        ev.append({"ph": "M", "name": kind, "pid": pid, "tid": tid, "args": args})

    meta("process_name", CPU_PID, 0, name="CPUs")
    meta("process_sort_index", CPU_PID, 0, sort_index=-1)
    for cpu in tl.cpus:
        meta("thread_name", CPU_PID, cpu, name="CPU %d" % cpu)
        meta("thread_sort_index", CPU_PID, cpu, sort_index=cpu * 2)
        meta("thread_name", CPU_PID, IRQ_TID_BASE + cpu, name="CPU %d interrupts" % cpu)
        meta("thread_sort_index", CPU_PID, IRQ_TID_BASE + cpu, sort_index=cpu * 2 + 1)

    for pid in sorted({k[0] for k in tl.states}):
        meta("process_name", pid, 0, name=tl.proc_label(pid))
    callers = {s.thread for s in tl.kercalls if s.thread is not None} if kercalls else set()
    for key in sorted(tl.states):
        meta("thread_name", key[0], key[1], name=tl.thread_label(key))
        meta("thread_sort_index", key[0], key[1], sort_index=key[1] * 2)
        if key in callers:
            meta("thread_name", key[0], CALL_TID_BASE + key[1],
                 name=tl.thread_label(key) + " kernel calls")
            meta("thread_sort_index", key[0], CALL_TID_BASE + key[1], sort_index=key[1] * 2 + 1)

    for key, ivs in sorted(tl.states.items()):
        who = "%s: %s" % (tl.proc_label(key[0]), tl.thread_label(key))
        for iv in ivs:
            args: Dict[str, object] = {"cpu": iv.cpu}
            if iv.priority is not None:
                args["priority"] = iv.priority
            ev.append({"ph": "X", "cat": "state", "name": iv.state, "pid": key[0],
                       "tid": key[1], "ts": _us(iv.start), "dur": _us(iv.dur), "args": args})
            if iv.state == m.RUNNING:
                ev.append({"ph": "X", "cat": "sched", "name": who, "pid": CPU_PID,
                           "tid": iv.cpu, "ts": _us(iv.start), "dur": _us(iv.dur),
                           "args": {"pid": key[0], "tid": key[1]}})

    for s in tl.interrupts:
        base = {"cat": "irq", "name": s.name, "pid": CPU_PID, "tid": IRQ_TID_BASE + s.cpu,
                "ts": _us(s.start), "args": _thread_args(tl, s.thread)}
        if s.end > s.start:
            ev.append(dict(base, ph="X", dur=_us(s.end - s.start)))
        else:
            ev.append(dict(base, ph="i", s="t"))

    if kercalls:
        for s in tl.kercalls:
            assert s.thread is not None
            ev.append({"ph": "X", "cat": "kercall", "name": s.name, "pid": s.thread[0],
                       "tid": CALL_TID_BASE + s.thread[1], "ts": _us(s.start),
                       "dur": _us(s.end - s.start)})

    for n, f in enumerate(tl.flows, 1):
        name = "MsgSend" if f.kind == "send" else "MsgReply"
        ev.append({"ph": "s", "cat": "ipc", "name": name, "id": n, "pid": f.src[0],
                   "tid": f.src[1], "ts": _us(f.start)})
        ev.append({"ph": "f", "bp": "e", "cat": "ipc", "name": name, "id": n,
                   "pid": f.dst[0], "tid": f.dst[1], "ts": _us(f.end)})

    for u in tl.user_events:
        label = str(u.args.get("text") or "") or "user event %s" % u.name
        ev.append({"ph": "i", "s": "g", "cat": "user", "name": label,
                   "pid": CPU_PID, "tid": u.cpu, "ts": _us(u.ts), "args": dict(u.args)})
    return ev


def _thread_args(tl: Timeline, key: "ThreadKey | None") -> dict:
    if key is None:
        return {}
    return {"interrupted": "%s: %s" % (tl.proc_label(key[0]), tl.thread_label(key))}


def write(tl: Timeline, out: IO[str], header: "Dict[str, str] | None" = None,
          kercalls: bool = True) -> int:
    events = to_events(tl, kercalls=kercalls)
    doc = {"displayTimeUnit": "ns", "traceEvents": events,
           "otherData": dict(header or {}, generator="kevscope")}
    json.dump(doc, out, separators=(",", ":"))
    return len(events)
