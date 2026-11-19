"""Command line: python3 -m kevscope {convert,stats} INPUT"""
from __future__ import annotations

import argparse
import sys

from . import blame, kev, perfetto, stats, textparse, timeline, view
from .model import Trace


def load(path: str) -> Trace:
    with open(path, "rb") as f:
        data = f.read()
    if textparse.looks_like_text(data):
        return textparse.parse(data.decode("utf-8", "replace"))
    return kev.parse(data)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="kevscope",
        description="View QNX 8.0 kernel event traces in Perfetto and print scheduling statistics.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("convert", help="write Trace Event JSON for ui.perfetto.dev")
    c.add_argument("input", help="traceprinter text output, or a raw .kev file")
    c.add_argument("-o", "--output", required=True)
    c.add_argument("--no-kercalls", action="store_true", help="leave kernel calls out")
    s = sub.add_parser("stats", help="print per-thread CPU time and scheduling latency")
    s.add_argument("input")
    s.add_argument("--top", type=int, default=20)
    s.add_argument("--idle", action="store_true", help="include the kernel's idle threads")
    b = sub.add_parser("blame", help="show which threads ran while one thread waited")
    b.add_argument("input")
    b.add_argument("--thread", required=True,
                   help="thread name, part of 'process: thread', or PID:TID")
    b.add_argument("--state", action="append",
                   help="waiting state to analyse (repeatable); default: Ready and mutex states")
    v = sub.add_parser("view", help="convert and open the trace in ui.perfetto.dev")
    v.add_argument("input")
    v.add_argument("-o", "--output", help="where to keep the JSON (default: INPUT.json)")
    v.add_argument("--no-browser", action="store_true", help="print the link instead of opening it")
    a = ap.parse_args(argv)

    try:
        trace = load(a.input)
    except kev.KevFormatError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    for w in trace.warnings[:10]:
        print("warning: " + w, file=sys.stderr)
    if len(trace.warnings) > 10:
        print("warning: %d more" % (len(trace.warnings) - 10), file=sys.stderr)
    if not trace.events:
        print("error: no events decoded from %s" % a.input, file=sys.stderr)
        return 1
    tl = timeline.build(trace)
    if a.cmd == "stats":
        print(stats.report(tl, a.top, a.idle))
    elif a.cmd == "blame":
        key, why = blame.pick(tl, a.thread)
        if key is None:
            print("error: " + why, file=sys.stderr)
            return 1
        print(blame.report(tl, key, tuple(a.state) if a.state else blame.DEFAULT_STATES))
    elif a.cmd == "view":
        out_path = a.output or a.input + ".json"
        with open(out_path, "w") as out:
            perfetto.write(tl, out, trace.header)
        if not view.serve_once(out_path, open_browser=not a.no_browser):
            print("The Perfetto UI did not fetch the trace; open %s in ui.perfetto.dev "
                  "with 'Open trace file'." % out_path, file=sys.stderr)
            return 1
    else:
        with open(a.output, "w") as out:
            n = perfetto.write(tl, out, trace.header, kercalls=not a.no_kercalls)
        print("%d events in, %d trace events out -> %s" % (len(trace.events), n, a.output))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
