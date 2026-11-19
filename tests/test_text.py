"""Tests for the traceprinter text decoder.

The fixture is hand-written in the line layout that QNX documents for
traceprinter (SAT User's Guide, "Using traceprinter"); it is NOT a capture
from a QNX system. One cycle is one nanosecond in it, and the low 32 bits of
the clock roll over half way through.
"""
import io
import json
import os
import tempfile
import unittest

from kevscope import __main__ as cli
from kevscope import kev, perfetto, stats, textparse, timeline

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "synthetic_traceprinter.txt")


def load():
    with open(FIXTURE) as f:
        return textparse.parse(f.read())


class TextDecoderTest(unittest.TestCase):
    def setUp(self):
        self.trace = load()
        self.by = {}
        for e in self.trace.events:
            self.by.setdefault((e.cls, e.name), []).append(e)

    def test_header(self):
        h = self.trace.header
        self.assertEqual(h["TRACE_CYCLES_PER_SEC"], "1000000000")
        self.assertEqual(h["TRACE_CPU_NUM"], "2")
        self.assertEqual(h["TRACE_ENCODING"], "16 byte events")
        self.assertTrue(h["TRACE_TRACELOGGER_ARGS"].startswith("hand-written"))

    def test_every_event_line_decoded(self):
        self.assertEqual(len(self.trace.events), 29)
        self.assertEqual(self.trace.warnings, [])

    def test_multi_line_process_name(self):
        names = {e.args["pid"]: e.args["name"] for e in self.by[("PROCESS", "PROCCREATE_NAME")]}
        self.assertEqual(names, {1: "proc/boot/procnto-smp-instr",
                                 4100: "usr/bin/demo server", 8200: "./client"})
        self.assertEqual(self.by[("PROCESS", "PROCCREATE_NAME")][1].args["ppid"], 1)

    def test_control_events(self):
        buf = self.by[("CONTROL", "BUFFER")][0]
        self.assertEqual((buf.args["sequence"], buf.args["num_events"]), (1, 29))
        t1, t2 = self.by[("CONTROL", "TIME")]
        self.assertEqual((t1.args["msb"], t2.args["msb"]), (1, 2))   # with and without comma

    def test_kernel_call_fields(self):
        call = self.by[("KER_CALL", "MSG_SENDV")][0]
        self.assertEqual((call.args["number"], call.args["coid"], call.args["msg"]), (11, 3, ""))
        exit_ = self.by[("KER_EXIT", "MSG_REPLYV")][0]
        self.assertEqual((exit_.args["number"], exit_.args["ret_val"]), (15, 0))

    def test_interrupt_and_comm_fields(self):
        entr = self.by[("INT_ENTR", "0x00000000")][0]
        self.assertEqual((entr.args["interrupt"], entr.args["IP"]), (0, 0xF008433E))
        self.assertEqual(self.by[("INT_HANDLER_EXIT", "0x00000000")][0].args["SIGEVENT"], "NONE")
        snd = self.by[("COMM", "SND_MESSAGE")][0]
        self.assertEqual((snd.args["rcvid"], snd.args["pid"]), (0x4F, 4100))

    def test_time_rebuilt_across_rollover(self):
        self.assertEqual(self.trace.events[0].ts, 0)
        self.assertEqual(self.by[("COMM", "REC_MESSAGE")][0].ts, 0x380)
        self.assertEqual(self.by[("INT_ENTR", "0x00000000")][0].ts, 0x600)
        self.assertEqual(self.trace.events[-1].ts, 0xD00)
        stamps = [e.ts for e in self.trace.events]
        self.assertEqual(stamps, sorted(stamps))

    def test_rollover_without_a_time_event(self):
        with open(FIXTURE) as f:
            lines = [l for l in f if "CONTROL :TIME" not in l]
        trace = textparse.parse("".join(lines))
        self.assertEqual(len(trace.events), 27)
        self.assertEqual(trace.events[-1].ts, 0xD00)

    def test_cycles_are_scaled_to_nanoseconds(self):
        with open(FIXTURE) as f:
            text = f.read().replace("1000000000", "500000000")
        self.assertEqual(textparse.parse(text).events[-1].ts, 0xD00 * 2)

    def test_full_64_bit_stamps_are_accepted(self):
        trace = textparse.parse(
            "  TRACE_CYCLES_PER_SEC:: 1000000000\n"
            "t:0x1fffffff0 CPU:00 THREAD  :THREADY       pid:5 tid:1\n"
            "t:0x200000010 CPU:00 THREAD  :THRUNNING     pid:5 tid:1\n")
        self.assertEqual([e.ts for e in trace.events], [0, 0x20])

    def test_junk_lines_are_reported(self):
        trace = textparse.parse("hello\nt:0x00000010 CPU:00 THREAD  :THREADY pid:5 tid:1\n")
        self.assertEqual(len(trace.events), 1)
        self.assertTrue(any("not understood" in w for w in trace.warnings))
        self.assertTrue(any("TRACE_CYCLES_PER_SEC" in w for w in trace.warnings))

    def test_detection(self):
        with open(FIXTURE, "rb") as f:
            self.assertTrue(textparse.looks_like_text(f.read()))
        self.assertFalse(textparse.looks_like_text(b"\x00\x01\x02\x03" * 64))


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tl = timeline.build(load())

    def test_thread_states(self):
        self.assertEqual([(i.state, i.start, i.end) for i in self.tl.states[(8200, 1)]], [
            ("Running", 0x70, 0x200), ("Reply-blocked", 0x200, 0x900),
            ("Ready", 0x900, 0xB00), ("Running", 0xB00, 0xC00)])
        self.assertEqual(self.tl.proc_label(4100), "demo server")

    def test_flows_interrupts_calls(self):
        send, reply = sorted(self.tl.flows, key=lambda f: f.start)
        self.assertEqual((send.start, send.src, send.end, send.dst),
                         (0x180, (8200, 1), 0x380, (4100, 1)))
        self.assertEqual((reply.start, reply.src, reply.end, reply.dst),
                         (0x880, (4100, 1), 0xB00, (8200, 1)))
        self.assertEqual(sorted((s.name, s.start, s.end) for s in self.tl.interrupts),
                         [("IRQ 0", 0x600, 0x780), ("IRQ 0 handler", 0x680, 0x700)])
        self.assertEqual(sorted((s.name, s.start, s.end, s.thread) for s in self.tl.kercalls),
                         [("MSG_REPLYV", 0x800, 0x980, (4100, 1)),
                          ("MSG_SENDV", 0x100, 0xB80, (8200, 1))])
        self.assertEqual(self.tl.unmatched, {})

    def test_latency(self):
        s = {x.key: x for x in stats.thread_stats(self.tl)}
        self.assertEqual((s[(8200, 1)].wakeups, s[(8200, 1)].lat_max), (1, 0x200))
        self.assertEqual((s[(4100, 1)].wakeups, s[(4100, 1)].lat_max), (1, 0x80))

    def test_cli_convert_and_stats(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "t.json")
            self.assertEqual(cli.main(["convert", FIXTURE, "-o", out]), 0)
            with open(out) as f:
                doc = json.load(f)
            self.assertEqual(doc["otherData"]["TRACE_SYSNAME"], "SYNTHETIC")
            names = {e["args"]["name"] for e in doc["traceEvents"]
                     if e["ph"] == "M" and e["name"] == "process_name"}
            self.assertEqual(names, {"CPUs", "procnto-smp-instr", "demo server", "client"})
            self.assertEqual(cli.main(["stats", FIXTURE]), 0)

    def test_cli_refuses_raw_kev(self):
        with tempfile.TemporaryDirectory() as d:
            raw = os.path.join(d, "x.kev")
            with open(raw, "wb") as f:
                f.write(b"\x00\x01\x02\x03" * 64)
            self.assertEqual(cli.main(["stats", raw]), 2)
        with self.assertRaises(kev.KevFormatError):
            kev.parse(b"\x00" * 32)


if __name__ == "__main__":
    unittest.main()
