"""Turn a flat event list into per-thread state intervals, interrupt and
kernel-call slices, and message-passing flows."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import model as m

ThreadKey = Tuple[int, int]  # (pid, tid)


@dataclass
class Interval:
    start: int
    end: int
    state: str
    cpu: int                 # CPU that logged the state change
    priority: Optional[int] = None

    @property
    def dur(self) -> int:
        return self.end - self.start


@dataclass
class Slice:
    start: int
    end: int
    name: str
    cpu: int
    thread: Optional[ThreadKey] = None


@dataclass
class Flow:
    """One message hand-off: where it left a thread and where it arrived."""
    kind: str                # "send" (client to server) or "reply"
    start: int
    src: ThreadKey
    end: int
    dst: ThreadKey


@dataclass
class Timeline:
    end_ts: int = 0
    proc_names: Dict[int, str] = field(default_factory=dict)
    thread_names: Dict[ThreadKey, str] = field(default_factory=dict)
    states: Dict[ThreadKey, List[Interval]] = field(default_factory=dict)
    interrupts: List[Slice] = field(default_factory=list)
    kercalls: List[Slice] = field(default_factory=list)
    flows: List[Flow] = field(default_factory=list)
    user_events: List[m.Event] = field(default_factory=list)
    cpus: List[int] = field(default_factory=list)
    unmatched: Dict[str, int] = field(default_factory=dict)

    def proc_label(self, pid: int) -> str:
        name = self.proc_names.get(pid)
        return name.rsplit("/", 1)[-1] if name else "pid %d" % pid

    def thread_label(self, key: ThreadKey) -> str:
        name = self.thread_names.get(key)
        return name if name else "tid %d" % key[1]


def _bump(d: Dict[str, int], key: str) -> None:
    d[key] = d.get(key, 0) + 1


def build(trace: m.Trace) -> Timeline:
    tl = Timeline()
    open_state: Dict[ThreadKey, Interval] = {}
    running_on: Dict[int, ThreadKey] = {}          # cpu -> thread now running
    open_irq: Dict[int, List[Tuple[int, str]]] = {}  # cpu -> stack of (ts, name)
    open_call: Dict[ThreadKey, Tuple[int, str, int]] = {}
    pending_send: Dict[int, Tuple[int, ThreadKey]] = {}   # rcvid -> (ts, client)
    replying: Dict[ThreadKey, Tuple[int, ThreadKey]] = {}  # client -> (ts, server)
    cpus = set()

    for ev in trace.events:
        cpus.add(ev.cpu)
        if ev.ts > tl.end_ts:
            tl.end_ts = ev.ts

        if ev.cls == m.PROCESS:
            if ev.name == "PROCCREATE_NAME" and "name" in ev.args:
                tl.proc_names[ev.get_int("pid")] = str(ev.args["name"])
            elif ev.name == "PROCTHREAD_NAME" and "name" in ev.args:
                key = (ev.get_int("pid"), ev.get_int("tid"))
                tl.thread_names[key] = str(ev.args["name"])

        elif ev.cls in (m.THREAD, m.VTHREAD):
            if ev.name == "THNAME" and "name" in ev.args:
                tl.thread_names[(ev.get_int("pid"), ev.get_int("tid"))] = str(ev.args["name"])
                continue
            state = m.thread_state(ev.name)
            if state is None:
                continue        # THCREATE / THDESTROY carry no state
            key = (ev.get_int("pid"), ev.get_int("tid"))
            prev = open_state.pop(key, None)
            if prev is not None:
                prev.end = ev.ts
                tl.states.setdefault(key, []).append(prev)
                if prev.state == m.RUNNING and running_on.get(prev.cpu) == key:
                    del running_on[prev.cpu]
            prio = ev.get_int("priority", -1)
            iv = Interval(ev.ts, ev.ts, state, ev.cpu, prio if prio >= 0 else None)
            if state != "Dead":
                open_state[key] = iv
            if state == m.RUNNING:
                running_on[ev.cpu] = key
                # A reply that made this thread runnable: the flow lands here.
                rep = replying.pop(key, None)
                if rep is not None:
                    tl.flows.append(Flow("reply", rep[0], rep[1], ev.ts, key))

        elif ev.cls == m.INT_ENTR or ev.cls == m.INT_HANDLER_ENTR:
            open_irq.setdefault(ev.cpu, []).append((ev.ts, _irq_name(ev)))
        elif ev.cls == m.INT_EXIT or ev.cls == m.INT_HANDLER_EXIT:
            stack = open_irq.get(ev.cpu)
            if stack:
                ts, name = stack.pop()
                tl.interrupts.append(Slice(ts, ev.ts, name, ev.cpu, running_on.get(ev.cpu)))
            else:
                _bump(tl.unmatched, "interrupt exit without entry")

        elif ev.cls == m.INTERRUPT:
            # QNX 8.0 logs one INT_DELIVER per interrupt; the work happens in
            # an interrupt service thread, visible as that thread's states.
            irq = ev.args.get("intr_id", "?")
            tl.interrupts.append(Slice(ev.ts, ev.ts, "IRQ %s" % irq, ev.cpu, running_on.get(ev.cpu)))

        elif ev.cls == m.KER_CALL:
            cur = running_on.get(ev.cpu)
            if cur is not None:
                open_call[cur] = (ev.ts, ev.name, ev.cpu)
        elif ev.cls == m.KER_EXIT:
            cur = running_on.get(ev.cpu)
            call = open_call.pop(cur, None) if cur is not None else None
            if call is not None and call[1] == ev.name:
                tl.kercalls.append(Slice(call[0], ev.ts, call[1], call[2], cur))
            elif cur is not None:
                _bump(tl.unmatched, "kernel exit without matching entry")

        elif ev.cls == m.COMM:
            cur = running_on.get(ev.cpu)
            if cur is None:
                _bump(tl.unmatched, "message event with no running thread known")
                continue
            if ev.name == "SND_MESSAGE":
                pending_send[ev.get_int("rcvid")] = (ev.ts, cur)
            elif ev.name == "REC_MESSAGE":
                snd = pending_send.pop(ev.get_int("rcvid"), None)
                if snd is not None:
                    tl.flows.append(Flow("send", snd[0], snd[1], ev.ts, cur))
                else:
                    _bump(tl.unmatched, "receive without a traced send")
            elif ev.name == "REPLY_MESSAGE":
                client = (ev.get_int("pid"), ev.get_int("tid"))
                replying[client] = (ev.ts, cur)

        elif ev.cls in (m.USER, m.USREVENT):
            ev.args["text"] = user_text(ev)
            ev.args["thread"] = _who(tl, running_on.get(ev.cpu))
            tl.user_events.append(ev)

    # Close whatever is still open at the end of the capture.
    for key, iv in open_state.items():
        iv.end = tl.end_ts
        tl.states.setdefault(key, []).append(iv)
    for key in tl.states:
        tl.states[key].sort(key=lambda i: i.start)
    tl.cpus = sorted(cpus)
    return tl


def clip(tl: Timeline, t0: int, t1: int) -> Timeline:
    """A copy of `tl` cut down to the window [t0, t1) nanoseconds, with times
    shifted so that the window starts at zero. Intervals that cross the edges
    are trimmed; flows are kept only if both ends are inside."""
    t0 = max(0, t0)
    t1 = min(max(t1, t0), tl.end_ts)
    out = Timeline(end_ts=t1 - t0, proc_names=dict(tl.proc_names),
                   thread_names=dict(tl.thread_names), cpus=list(tl.cpus),
                   unmatched=dict(tl.unmatched))
    for key, ivs in tl.states.items():
        kept = [Interval(max(iv.start, t0) - t0, min(iv.end, t1) - t0, iv.state, iv.cpu, iv.priority)
                for iv in ivs if iv.end > t0 and iv.start < t1]
        if kept:
            out.states[key] = kept

    def cut(slices: List[Slice]) -> List[Slice]:
        return [Slice(max(s.start, t0) - t0, min(s.end, t1) - t0, s.name, s.cpu, s.thread)
                for s in slices if s.end >= t0 and s.start < t1 and (s.end > t0 or s.start >= t0)]

    out.interrupts = cut(tl.interrupts)
    out.kercalls = [s for s in cut(tl.kercalls) if s.thread in out.states]
    out.flows = [Flow(f.kind, f.start - t0, f.src, f.end - t0, f.dst)
                 for f in tl.flows if f.start >= t0 and f.end < t1]
    for u in tl.user_events:
        if t0 <= u.ts < t1:
            out.user_events.append(m.Event(u.ts - t0, u.cpu, u.cls, u.name, dict(u.args)))
    return out


def _who(tl: "Timeline", key: Optional[ThreadKey]) -> str:
    return "" if key is None else "%s: %s" % (tl.proc_label(key[0]), tl.thread_label(key))


def user_text(ev: m.Event) -> str:
    """Text of a user event: traceprinter's STR field when it printed one,
    otherwise the d0, d1, ... data words read as little-endian characters."""
    if isinstance(ev.args.get("STR"), str):
        return str(ev.args["STR"])
    raw = bytearray()
    i = 0
    while isinstance(ev.args.get("d%d" % i), int):
        raw += int(ev.args["d%d" % i]).to_bytes(4, "little", signed=False)
        i += 1
    text = bytes(raw).split(b"\0", 1)[0]
    if text and all(32 <= b < 127 for b in text):
        return text.decode("ascii")
    return ""


def _irq_name(ev: m.Event) -> str:
    irq = ev.args.get("interrupt", ev.name)
    label = "IRQ %s" % irq
    return label if ev.cls == m.INT_ENTR else label + " handler"
