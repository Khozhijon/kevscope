"""Tests for the format-independent core, on hand-built model events."""
import io
import json
import unittest

from kevscope import model as m
from kevscope import perfetto, stats, timeline


def E(ts, cpu, cls, ev, /, **args):
    return m.Event(ts, cpu, cls, ev, dict(args))


def scenario():
    """Client (pid 20 tid 1) sends to server (pid 10 tid 2) on one CPU, with an
    interrupt while the server works."""
    return m.Trace(events=[
        E(0, 0, m.PROCESS, "PROCCREATE_NAME", ppid=1, pid=10, name="proc/boot/server"),
        E(0, 0, m.PROCESS, "PROCCREATE_NAME", ppid=1, pid=20, name="/tmp/client"),
        E(0, 0, m.PROCESS, "PROCTHREAD_NAME", pid=10, tid=2, name="worker"),
        E(100, 0, m.THREAD, "THRECEIVE", pid=10, tid=2),
        E(100, 0, m.THREAD, "THREADY", pid=20, tid=1, priority=10),
        E(400, 0, m.THREAD, "THRUNNING", pid=20, tid=1, priority=10),
        E(500, 0, m.KER_CALL, "MSG_SENDV", coid=3),
        E(600, 0, m.COMM, "SND_MESSAGE", rcvid=0x77, pid=10),
        E(650, 0, m.THREAD, "THREPLY", pid=20, tid=1),
        E(650, 0, m.THREAD, "THREADY", pid=10, tid=2, priority=20),
        E(700, 0, m.THREAD, "THRUNNING", pid=10, tid=2, priority=20),
        E(750, 0, m.COMM, "REC_MESSAGE", rcvid=0x77, pid=20),
        E(800, 0, m.INT_ENTR, "0x00000001", interrupt="0x00000001"),
        E(900, 0, m.INT_EXIT, "0x00000001", interrupt="0x00000001"),
        E(1000, 0, m.COMM, "REPLY_MESSAGE", tid=1, pid=20),
        E(1100, 0, m.THREAD, "THREADY", pid=20, tid=1, priority=10),
        E(1200, 0, m.THREAD, "THRECEIVE", pid=10, tid=2),
        E(2100, 0, m.THREAD, "THRUNNING", pid=20, tid=1, priority=10),
        E(2200, 0, m.KER_EXIT, "MSG_SENDV", ret_val=0),
        E(3000, 0, m.THREAD, "THDEAD", pid=20, tid=1),
    ])


class TimelineTest(unittest.TestCase):
    def setUp(self):
        self.tl = timeline.build(scenario())

    def test_names(self):
        self.assertEqual(self.tl.proc_label(10), "server")
        self.assertEqual(self.tl.proc_label(99), "pid 99")
        self.assertEqual(self.tl.thread_label((10, 2)), "worker")
        self.assertEqual(self.tl.thread_label((20, 1)), "tid 1")

    def test_state_intervals_are_contiguous(self):
        ivs = self.tl.states[(20, 1)]
        self.assertEqual([(i.state, i.start, i.end) for i in ivs], [
            ("Ready", 100, 400), ("Running", 400, 650), ("Reply-blocked", 650, 1100),
            ("Ready", 1100, 2100), ("Running", 2100, 3000)])
        for a, b in zip(ivs, ivs[1:]):
            self.assertEqual(a.end, b.start)

    def test_open_interval_closed_at_trace_end(self):
        last = self.tl.states[(10, 2)][-1]
        self.assertEqual((last.state, last.start, last.end), ("Receive-blocked", 1200, 3000))

    def test_interrupt_slice_knows_interrupted_thread(self):
        (irq,) = self.tl.interrupts
        self.assertEqual((irq.start, irq.end, irq.thread), (800, 900, (10, 2)))

    def test_kernel_call_spans_the_block(self):
        (call,) = self.tl.kercalls
        self.assertEqual((call.name, call.start, call.end, call.thread),
                         ("MSG_SENDV", 500, 2200, (20, 1)))

    def test_message_flows(self):
        send, reply = sorted(self.tl.flows, key=lambda f: f.start)
        self.assertEqual((send.kind, send.start, send.src, send.end, send.dst),
                         ("send", 600, (20, 1), 750, (10, 2)))
        self.assertEqual((reply.kind, reply.start, reply.src, reply.end, reply.dst),
                         ("reply", 1000, (10, 2), 2100, (20, 1)))

    def test_unpaired_events_are_counted_not_fatal(self):
        tl = timeline.build(m.Trace(events=[
            E(5, 0, m.INT_EXIT, "0x0", interrupt="0x0"),
            E(6, 0, m.COMM, "SND_MESSAGE", rcvid=1, pid=1),
        ]))
        self.assertEqual(tl.unmatched["interrupt exit without entry"], 1)
        self.assertEqual(tl.unmatched["message event with no running thread known"], 1)


class StatsTest(unittest.TestCase):
    def test_latency(self):
        tl = timeline.build(scenario())
        by_key = {s.key: s for s in stats.thread_stats(tl)}
        c = by_key[(20, 1)]
        self.assertEqual((c.wakeups, c.lat_min, c.lat_max, c.lat_avg), (2, 300, 1000, 650))
        self.assertEqual(c.lat_max_at, 1100)
        self.assertEqual(c.run_ns, 250 + 900)
        s = by_key[(10, 2)]
        self.assertEqual((s.wakeups, s.lat_max, s.run_ns), (1, 50, 500))

    def test_percentile_nearest_rank(self):
        vals = list(range(1, 101))
        self.assertEqual(stats.percentile(vals, 99), 99)
        self.assertEqual(stats.percentile(vals, 100), 100)
        self.assertEqual(stats.percentile([7], 99), 7)
        self.assertEqual(stats.percentile([], 99), 0)

    def test_report_renders(self):
        text = stats.report(timeline.build(scenario()))
        self.assertIn("client", text)
        self.assertIn("2 message flows", text)

    def test_fmt(self):
        self.assertEqual(stats.fmt_ns(999), "999 ns")
        self.assertEqual(stats.fmt_ns(1500), "1.5 us")
        self.assertEqual(stats.fmt_ns(2_500_000), "2.500 ms")


class PerfettoTest(unittest.TestCase):
    def setUp(self):
        buf = io.StringIO()
        self.count = perfetto.write(timeline.build(scenario()), buf, {"TRACE_CPU_NUM": "1"})
        self.doc = json.loads(buf.getvalue())
        self.ev = self.doc["traceEvents"]

    def test_document_shape(self):
        self.assertEqual(self.count, len(self.ev))
        self.assertEqual(self.doc["otherData"]["TRACE_CPU_NUM"], "1")
        for e in self.ev:
            self.assertIn(e["ph"], "MXsfi")
            self.assertIsInstance(e["pid"], int)
            self.assertIsInstance(e["tid"], int)
            if e["ph"] != "M":
                self.assertGreaterEqual(e["ts"], 0)

    def test_times_are_microseconds(self):
        run = [e for e in self.ev if e["ph"] == "X" and e["cat"] == "state"
               and e["pid"] == 20 and e["name"] == "Running"][0]
        self.assertEqual((run["ts"], run["dur"]), (0.4, 0.25))

    def test_slices_on_one_track_do_not_overlap(self):
        tracks = {}
        for e in self.ev:
            if e["ph"] == "X":
                tracks.setdefault((e["pid"], e["tid"]), []).append((e["ts"], e["ts"] + e["dur"]))
        self.assertGreaterEqual(len(tracks), 5)   # 2 threads, 1 call track, CPU 0, its interrupts
        for spans in tracks.values():
            spans.sort()
            for (_, end), (start, _) in zip(spans, spans[1:]):
                self.assertLessEqual(end, start + 1e-9)

    def test_cpu_track_mirrors_running(self):
        cpu = [e for e in self.ev if e["ph"] == "X" and e["pid"] == perfetto.CPU_PID
               and e["tid"] == 0]
        self.assertEqual([e["name"] for e in sorted(cpu, key=lambda e: e["ts"])],
                         ["client: tid 1", "server: worker", "client: tid 1"])

    def test_flows_come_in_pairs(self):
        starts = {e["id"]: e for e in self.ev if e["ph"] == "s"}
        ends = {e["id"]: e for e in self.ev if e["ph"] == "f"}
        self.assertEqual(set(starts), set(ends))
        self.assertEqual(len(starts), 2)
        for i in starts:
            self.assertLessEqual(starts[i]["ts"], ends[i]["ts"])

    def test_kernel_calls_get_their_own_track_under_the_thread(self):
        (call,) = [e for e in self.ev if e.get("cat") == "kercall"]
        self.assertEqual((call["ph"], call["name"], call["pid"], call["tid"]),
                         ("X", "MSG_SENDV", 20, perfetto.CALL_TID_BASE + 1))
        self.assertEqual((call["ts"], call["dur"]), (0.5, 1.7))
        names = {(e["pid"], e["tid"]): e["args"]["name"] for e in self.ev
                 if e["ph"] == "M" and e["name"] == "thread_name"}
        self.assertEqual(names[(20, perfetto.CALL_TID_BASE + 1)], "tid 1 kernel calls")
        order = {(e["pid"], e["tid"]): e["args"]["sort_index"] for e in self.ev
                 if e["ph"] == "M" and e["name"] == "thread_sort_index" and e["pid"] == 20}
        self.assertEqual(order[(20, perfetto.CALL_TID_BASE + 1)], order[(20, 1)] + 1)


if __name__ == "__main__":
    unittest.main()
