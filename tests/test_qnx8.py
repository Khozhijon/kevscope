"""Tests for the line forms a real QNX 8.0 traceprinter prints (wide mode),
on a hand-written fixture in that layout, and for the blame analysis."""
import io
import json
import os
import unittest

from kevscope import __main__ as cli
from kevscope import blame, perfetto, stats, textparse, timeline, view

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "synthetic_qnx8.txt")
LOW, MID, HIGH, IDLE1 = (500, 2), (500, 3), (500, 4), (1, 2)


def load():
    with open(FIXTURE) as f:
        return textparse.parse(f.read())


class Qnx8DecoderTest(unittest.TestCase):
    def setUp(self):
        self.trace = load()
        self.tl = timeline.build(self.trace)

    def test_everything_decoded(self):
        self.assertEqual(self.trace.warnings, [])
        self.assertEqual(len(self.trace.events), 28)
        self.assertEqual(self.trace.header["TRACE_VER_MAJOR"], "2")

    def test_full_64_bit_timestamps(self):
        self.assertEqual(self.trace.events[0].ts, 0)
        self.assertEqual(self.trace.events[-1].ts, 0x1400)

    def test_wide_thread_fields(self):
        run = [i for i in self.tl.states[HIGH] if i.state == "Running"][0]
        self.assertEqual((run.start, run.end, run.priority, run.cpu), (0x400, 0x600, 18, 0))

    def test_new_and_unknown_states(self):
        self.assertEqual([i.state for i in self.tl.states[HIGH]],
                         ["Running", "Mutex-blocked", "Ready", "Running", "Tracebuffer-wait"])
        self.assertEqual(self.tl.states[IDLE1][-1].state, "Muon-mutex-blocked")
        self.assertEqual(self.tl.states[LOW][-1].state, "Futurestate")   # not in any table

    def test_interrupt_delivery_is_an_instant(self):
        (irq,) = self.tl.interrupts
        self.assertEqual((irq.name, irq.start, irq.end, irq.thread), ("IRQ 27", 0x300, 0x300, LOW))

    def test_user_events_text(self):
        self.assertEqual([(u.ts, u.args["text"], u.args["thread"]) for u in self.tl.user_events],
                         [(0x200, "start", "demo: low"), (0x1300, "stop", "demo: low")])

    def test_unindented_continuation_lines(self):
        call = [e for e in self.trace.events if e.cls == "KER_CALL"][0]
        self.assertEqual(call.args["sync_p"], 0x1234567890)
        self.assertEqual(call.args["event->sigev_notify_function_p"], 0)
        (kc,) = self.tl.kercalls
        self.assertEqual((kc.name, kc.start, kc.end, kc.thread),
                         ("SYNC_MUTEX_LOCK", 0x500, 0x1100, HIGH))

    def test_perfetto_output(self):
        buf = io.StringIO()
        perfetto.write(self.tl, buf, self.trace.header)
        ev = json.loads(buf.getvalue())["traceEvents"]
        irq = [e for e in ev if e.get("cat") == "irq"]
        self.assertEqual([(e["ph"], e["name"]) for e in irq], [("i", "IRQ 27")])
        self.assertEqual(sorted(e["name"] for e in ev if e.get("cat") == "user"), ["start", "stop"])
        prio = [e["args"]["priority"] for e in ev
                if e.get("cat") == "state" and e["pid"] == 500 and e["tid"] == 3]
        self.assertEqual(prio, [12, 12])


class BlameTest(unittest.TestCase):
    def setUp(self):
        self.tl = timeline.build(load())

    def test_inversion_is_attributed(self):
        (b,) = blame.blame(self.tl, HIGH, ("Mutex-blocked",))
        self.assertEqual((b.waits, b.total, b.longest, b.longest_at), (1, 0xA00, 0xA00, 0x600))
        self.assertEqual(b.ran, {MID: 0x800, LOW: 0x200})
        self.assertEqual(b.covered, 0xA00)

    def test_only_the_cpu_waited_for_counts(self):
        (ready,) = blame.blame(self.tl, HIGH, ("Ready",))
        self.assertEqual((ready.total, ready.ran), (0x80, {}))   # CPU 1's idle thread is not charged
        self.assertNotIn(IDLE1, blame.blame(self.tl, HIGH, ("Mutex-blocked",))[0].ran)

    def test_thread_lookup(self):
        self.assertEqual(blame.find_threads(self.tl, "high"), [HIGH])
        self.assertEqual(blame.find_threads(self.tl, "500:3"), [MID])
        self.assertEqual(blame.find_threads(self.tl, "demo"), [LOW, MID, HIGH])
        self.assertEqual(blame.pick(self.tl, "nobody")[0], None)
        key, why = blame.pick(self.tl, "demo")
        self.assertIsNone(key)
        self.assertIn("3 threads match", why)

    def test_report_text(self):
        text = blame.report(self.tl, HIGH)
        self.assertIn("Mutex-blocked: 1 time, ", text)
        self.assertIn("nothing else ran", text)
        self.assertIn("demo: mid", text)
        self.assertIn("80.0%", text)
        self.assertIn("never in", blame.report(self.tl, MID, ("Mutex-blocked",)))

    def test_cli(self):
        self.assertEqual(cli.main(["blame", FIXTURE, "--thread", "high"]), 0)
        self.assertEqual(cli.main(["blame", FIXTURE, "--thread", "nobody"]), 1)

    def test_idle_hidden_from_stats_by_default(self):
        self.assertNotIn("idle_cpu_1", stats.report(self.tl))
        self.assertIn("idle_cpu_1", stats.report(self.tl, show_idle=True))


class ViewTest(unittest.TestCase):
    def test_link_points_at_the_local_server(self):
        self.assertEqual(view.ui_url("t.json"),
                         "https://ui.perfetto.dev/#!/?url=http://127.0.0.1:9001/t.json")


if __name__ == "__main__":
    unittest.main()
