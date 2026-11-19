"""Format-independent event model.

Every input decoder (traceprinter text, raw .kev) produces a `Trace`: a header
dictionary and a list of `Event` objects whose timestamps are already
nanoseconds from the first event. Everything downstream (timeline building,
statistics, Perfetto export) depends only on this module.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Union

Value = Union[int, str]

# Trace classes, named as traceprinter prints them.
CONTROL = "CONTROL"
PROCESS = "PROCESS"
THREAD = "THREAD"
VTHREAD = "VTHREAD"
KER_CALL = "KER_CALL"
KER_EXIT = "KER_EXIT"
INT_ENTR = "INT_ENTR"
INT_EXIT = "INT_EXIT"
INT_HANDLER_ENTR = "INT_HANDLER_ENTR"
INT_HANDLER_EXIT = "INT_HANDLER_EXIT"
COMM = "COMM"
SYSTEM = "SYSTEM"
USER = "USER"
USREVENT = "USREVENT"      # how QNX 8.0 traceprinter labels user events
INTERRUPT = "INTERRUPT"    # QNX 8.0: one INT_DELIVER event per interrupt

# Thread-state events of the THREAD class, mapped to a readable state label.
# The names are the ones QNX documents for the thread states (STATE_* in
# <sys/states.h>, printed with a TH prefix by traceprinter).
THREAD_STATES: Dict[str, str] = {
    "THDEAD": "Dead",
    "THRUNNING": "Running",
    "THREADY": "Ready",
    "THSTOPPED": "Stopped",
    "THSEND": "Send-blocked",
    "THRECEIVE": "Receive-blocked",
    "THREPLY": "Reply-blocked",
    "THSTACK": "Stack-blocked",
    "THWAITTHREAD": "Waitthread-blocked",
    "THWAITPAGE": "Waitpage-blocked",
    "THSIGSUSPEND": "Sigsuspend-blocked",
    "THSIGWAITINFO": "Sigwaitinfo-blocked",
    "THNANOSLEEP": "Nanosleep-blocked",
    "THMUTEX": "Mutex-blocked",
    "THCONDVAR": "Condvar-blocked",
    "THJOIN": "Join-blocked",
    "THINTR": "Intr-blocked",
    "THSEM": "Sem-blocked",
    "THWAITCTX": "Waitctx-blocked",
    "THNET_SEND": "Net-send-blocked",
    "THNET_REPLY": "Net-reply-blocked",
    # Seen in real QNX 8.0 captures, not in the public event table:
    "THTRACEBUFFER": "Tracebuffer-wait",
    "THMUON_MUTEX": "Muon-mutex-blocked",
    "THSEND_NOTIFY": "Send-notify-blocked",
}

# THREAD events that are not states.
THREAD_NON_STATES = frozenset(("THCREATE", "THDESTROY", "THNAME"))


def thread_state(event_name: str) -> "str | None":
    """State label for a THREAD event, or None if it is not a state change.
    Unknown TH* names are still treated as states so that a newer kernel's
    additions show up on the timeline instead of silently extending the
    previous state."""
    label = THREAD_STATES.get(event_name)
    if label is None and event_name.startswith("TH") and event_name not in THREAD_NON_STATES:
        label = event_name[2:].capitalize()
    return label

RUNNING = "Running"
READY = "Ready"


@dataclass
class Event:
    ts: int                 # nanoseconds since the first event of the trace
    cpu: int
    cls: str                # one of the class names above
    name: str               # event name within the class, e.g. "THRUNNING"
    args: Dict[str, Value] = field(default_factory=dict)

    def get_int(self, key: str, default: int = 0) -> int:
        v = self.args.get(key, default)
        if isinstance(v, int):
            return v
        try:
            return int(str(v), 0)
        except ValueError:
            return default


@dataclass
class Trace:
    header: Dict[str, str] = field(default_factory=dict)
    events: List[Event] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
