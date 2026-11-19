# Find a priority inversion on QNX 8.0 from a Mac

This tutorial takes you from nothing to a QNX 8.0 system running in QEMU on an Apple-silicon Mac, and then to a kernel trace of a real priority inversion that you view in a browser and name from the command line. Built with help from Claude Code (Anthropic).

## What you will build and learn

You will:

- install QNX SDP 8.0 on a Mac even though it has no macOS host tools, by using a Linux container;
- build a QNX 8.0 image and boot it in QEMU;
- read a small QNX program that uses message passing, pulses and a mutex;
- capture a kernel trace with `tracelogger`, print it with `traceprinter`, and open it in Perfetto;
- use `kevscope blame` to find which thread kept a high-priority thread waiting;
- fix the inversion with `PTHREAD_PRIO_INHERIT` and see what changes.

You need no earlier QNX experience. You should know C and what a mutex is.

## Prerequisites

- An Apple-silicon Mac. This was tested on one Mac only: Apple M5, macOS 27.2.
- Docker Desktop 29.9 or similar, running, with Rosetta enabled (the SDP host tools are x86-64 Linux).
- Homebrew.
- A free myQNX account and a QNX Everywhere licence (Step 1).
- About 6 GB of free disk. The SDP baseline alone is 3.6 GB.
- Python 3.9 or newer.

This tutorial uses the kevscope repository for its scripts. Clone it first [TODO: repo URL] and run every command from its top directory.

Install the two Homebrew packages:

```bash
brew install qemu sshpass
```

## Step 1: get the licence

1. Create a free myQNX account.
2. At qnx.com/products/everywhere, accept the "QNX Development License Agreement (Non-Commercial)".
3. In the myQNX License Manager, choose Accept License and deploy it to your own e-mail address.
4. While signed in, download "QNX Software Center 2.0.4 - Linux Hosts". It is a `.run` file.

Use a password with letters and digits only. A password with other characters was rejected by the Software Center login in testing.

The licence limits what you may publish. Do not post timings or other measurements made on QNX.

You should now have: a myQNX account, an accepted licence, and the `qnx-setup-2.0.4-...-linux.run` file in `~/Downloads`.

## Step 2: install the SDP in a container

Why a container: QNX ships no macOS host tools for SDP 8.0 and no macOS Software Center. The Linux build runs fine in a linux/amd64 container. `docker/sdp.sh` wraps that container.

It uses a fixed hostname and MAC address. The licence activation ties to the machine, so a stable identity means you do not activate a "new machine" on each run.

Everything QNX installs goes to `~/qnx-linux`, outside the repository.

Install the Software Center:

```bash
docker/sdp.sh setup ~/Downloads/qnx-setup-2.0.4-202501021438-linux.run
```

Log in. A macOS dialog asks for your myQNX e-mail and password. They are stored with mode 0600 until you log out.

```bash
docker/sdp.sh login
```

Sync and activate your licence keys:

```bash
docker/sdp.sh qsc -syncLicenseKeys -activateAll -listLicenseKeys
```

Mirror the package list:

```bash
docker/sdp.sh qsc -mirror
```

Install the baseline (3.6 GB, about 6 minutes):

```bash
docker/sdp.sh qsc -destination /root/qnx800 -installBaseline com.qnx.qnx800
```

Install the QEMU target and virtio packages:

```bash
docker/sdp.sh qsc -destination /root/qnx800 -installPackage com.qnx.qnx800.target.qemuvirt,com.qnx.qnx800.target.driver.virtio,com.qnx.qnx800.target.driver.virtio.startup,com.qnx.qnx800.target.driver.virtio.devb,com.qnx.qnx800.target.driver.virtio.devc,com.qnx.qnx800.target.net.devsvirtio
```

Pitfall: if you skip `target.qemuvirt`, the next step fails with `Host file 'startup-qemu-virt' not available`. That package holds the QEMU startup code.

You should now have: SDP 8.0 under `~/qnx-linux/qnx800`.

## Step 3: build a QEMU image

`mkqnximage` builds a bootable image for the QEMU `virt` machine. Run it inside the container with the SDP environment loaded.

```bash
docker/sdp.sh sh bash -c '. /root/qnx800/qnxsdp-env.sh; mkdir -p /root/targets/a64 && cd /root/targets/a64 && mkqnximage --type=qemu --arch=aarch64le --hostname=qnx-a64 --build'
```

The image contains an SSH server and a local test account, root with password root. This is for a throwaway QEMU guest only.

You no longer need your QNX credentials. Remove them:

```bash
docker/sdp.sh logout
```

You should now have: `~/qnx-linux/home/targets/a64/output/ifs.bin` and `disk-qemu`.

## Step 4: boot it

```bash
tools/qemu-run.sh
```

The script runs `qemu-system-aarch64` with `-accel tcg -machine virt-8.2 -cpu cortex-a57 -smp 2 -m 1G`. It forwards the guest's SSH port to 127.0.0.1:2222. The serial console is your terminal.

You should see boot output and then a line like this:

```
QNX qnx-a64 8.0.0 2026/10/30-09:09:47EDT QEMU_virt aarch64le
```

To quit QEMU, press Ctrl-A, then X.

Why TCG and not HVF: Apple's hardware acceleration (HVF) did not work. QNX's `startup-qemu-virt` asserts in `gic_v3.c` under HVF, and QEMU 11 refuses GICv2 under HVF. TCG is software emulation. It is slow but it boots. Why `virt-8.2`: QEMU 11 no longer offers `virt-4.2`, which `mkqnximage`'s own runner asks for.

Leave this terminal running. Open a second terminal for the rest.

You should now have: a running QNX 8.0 guest, reachable at 127.0.0.1:2222.

## Step 5: read the workload

`workload/rtdemo.c` is a small program made to be traced. It starts threads pinned to CPU 0 so they compete. Three excerpts matter.

First, the server. It sits in a `MsgReceive` loop and answers requests. Rcvid 0 means a pulse, not a message.

```c
        rcvid_t rcvid = MsgReceive(chid, &msg, sizeof msg, NULL);
        if (rcvid == -1) {
            break;
        }
        if (rcvid == 0) {                 /* a pulse */
```

```c
        if (msg.req.type == MSG_WORK) {
            burn_us(msg.req.work_us);
            rep.served++;
            MsgReply(rcvid, EOK, &rep, sizeof rep);
```

The `sensor` (priority 15, every 5 ms) and `logger` (priority 8, every 20 ms) threads attach to the channel and call `MsgSend`, which blocks until the server replies. In the trace these show as Reply-blocked, with a flow arrow in each direction. A 10 ms POSIX timer delivers a pulse to the same channel, so the server also wakes on a timer tick. The `hog` thread burns 3 ms of CPU every 50 ms.

Second, the inversion. With `-i`, three more threads share one mutex. The low-priority one takes the lock and wakes the other two:

```c
        pthread_mutex_lock(&shared_lock);
        burn_us(1000);
        sem_post(&go_high);               /* inv-high preempts us and blocks on the lock */
        sem_post(&go_mid);                /* inv-mid preempts us unless we inherited prio 18 */
        burn_us(4000);                    /* rest of the critical section */
        pthread_mutex_unlock(&shared_lock);
```

`inv-low` has priority 5, `inv-mid` 12, `inv-high` 18. `inv-high` preempts `inv-low` and blocks on the lock. `inv-mid` needs no lock, but it outranks `inv-low`, so it runs for 10 ms. `inv-high` waits on the lock, and the thread that holds the lock cannot run. That is a priority inversion.

Third, the mutex protocol, chosen on the command line:

```c
        pthread_mutexattr_setprotocol(&ma, inherit ? PTHREAD_PRIO_INHERIT : PTHREAD_PRIO_NONE);
```

The program also writes user trace events with `trace_logf`, so the start and stop show up as markers in the timeline.

You should now have: a picture of who sends to whom, and which three threads cause the inversion.

## Step 6: capture a trace

First build `rtdemo` with the QNX toolchain in the container:

```bash
docker/sdp.sh sh bash -c '. /root/qnx800/qnxsdp-env.sh >/dev/null; make -C workload'
```

Copy it to the target:

```bash
tools/target.sh put workload/build/aarch64le/rtdemo /data/home/root/
```

`tools/trace-run.sh NAME SECONDS 'COMMAND'` does the capture. Run it for the inversion case:

```bash
mkdir -p captures
```

```bash
tools/trace-run.sh inversion 3 '/data/home/root/rtdemo -t 2 -i'
```

What it does, in order:

1. On the target it starts `tracelogger -w -s 3 -f /dev/shmem/inversion.kev`. The `-w` flag is wide mode. It adds thread priorities and more kernel-call arguments to the trace. `blame` works without it, but the priorities make the timeline easier to read.
2. It runs `rtdemo -t 2 -i`.
3. It copies `inversion.kev` back into `captures/`.
4. In the container it runs `traceprinter -f captures/inversion.kev -o captures/inversion.txt`.
5. It runs `kevscope convert` and `kevscope stats` on the text.

kevscope reads the text, not the raw `.kev`. QNX does not publish the binary layout, so kevscope does not guess.

`rtdemo` prints a line like `rtdemo: ran 2 s on QNX, 7 threads, mutex without priority inheritance`. It ran seven threads: the four base threads plus the three inversion threads.

You should now have: `captures/inversion.kev`, `captures/inversion.txt` and `captures/inversion.json`.

## Step 7: look at it in Perfetto

Open https://ui.perfetto.dev, choose "Open trace file", and pick `captures/inversion.json`. (`python3 -m kevscope view captures/inversion.txt` serves the file locally instead. That path was not confirmed end to end, because Chrome asks for a local-network permission that needs a click. The "Open trace file" route was confirmed.)

[TODO: add docs/img/perfetto-rtdemo.png, the rtdemo trace in Perfetto]

What each track shows:

- "CPUs" group: for each CPU, which thread ran, and a track of interrupt deliveries.
- One group per QNX process. For each thread, a state track (Running, Ready, Receive-blocked, Reply-blocked, Mutex-blocked, Nanosleep-blocked, Sem-blocked). Below it, a "kernel calls" track with MSG_SENDV, SYNC_MUTEX_LOCK and similar.
- Message flows as arrows: client MsgSend to server receive, and server MsgReply back to the client. The arrows were imported but I have not inspected them visually, so check them yourself.
- Markers for the `trace_logf` events.

Find the inversion. Expand the `rtdemo` group. Look at the `inv-high` state track for a Mutex-blocked slice. Then look at the `inv-mid` state track. During that Mutex-blocked slice, `inv-mid` shows Running. Click either slice to see the thread name and priority in its details.

You should now have: a Mutex-blocked slice on `inv-high` that lines up with a Running slice on `inv-mid`.

## Step 8: let the tool name the culprit

Looking works, but you can ask the question directly:

```bash
python3 -m kevscope blame captures/inversion.txt --thread inv-high --state Mutex-blocked
```

I cannot show the real output here, because the QNX licence does not allow publishing measurements. This is the same command on the hand-written test fixture, not measured on QNX:

```
demo: high (pid 500 tid 4)

  Ready: 1 time, 128 ns in total, longest 128 ns at t=4.1 us
  nothing else ran on the CPU it was waiting for (not traced, or the wait was a hand-over between events)

  Mutex-blocked: 1 time, 2.6 us in total, longest 2.6 us at t=1.5 us
  meanwhile the CPU it was waiting for ran:
    demo: mid                                        2.0 us   80.0%
    demo: low                                        512 ns   20.0%
```

How to read it. The header names the thread. For each waiting state it gives the number of waits and the total and longest time. Then it lists who ran on the CPU the thread was waiting for, with time and share. If an unrelated thread (`mid`) takes most of a mutex wait, and it does not hold the lock, you have a priority inversion. `low`, which holds the lock, should be the only thing running. When it is not, something is wrong.

On the real trace of `rtdemo -i`, the picture is the same: most of `inv-high`'s mutex wait is spent with `inv-mid` on the CPU.

`--thread` also accepts part of "process: thread" or PID:TID. Without `--state`, it analyses Ready and the mutex states.

You should now have: a `blame` report that lists `inv-mid` as the main thing on the CPU during `inv-high`'s Mutex-blocked wait.

## Step 9: fix it

The fix is one line. Give the mutex the priority-inheritance protocol. `rtdemo` does this with the `-p` flag, which switches `PTHREAD_PRIO_NONE` to `PTHREAD_PRIO_INHERIT`:

```c
        pthread_mutexattr_setprotocol(&ma, inherit ? PTHREAD_PRIO_INHERIT : PTHREAD_PRIO_NONE);
```

In your own code the fix is `pthread_mutexattr_setprotocol(&ma, PTHREAD_PRIO_INHERIT);`. A thread that holds the lock now runs at the priority of the highest-priority thread waiting for it.

Trace the fixed case:

```bash
tools/trace-run.sh inherit 3 '/data/home/root/rtdemo -t 2 -i -p'
```

```bash
python3 -m kevscope blame captures/inherit.txt --thread inv-high --state Mutex-blocked
```

What changes, qualitatively. In `blame`, `inv-mid` no longer takes most of the wait. Only the lock holder `inv-low` runs. In the timeline, `inv-low` inherits priority 18 while it holds the lock, so `inv-mid` can no longer cut in. The Mutex-blocked slice on `inv-high` no longer lines up with a Running slice on `inv-mid`.

`tools/demo.sh` does both runs and both `blame` reports in one go.

You should now have: a second trace in which `blame` shows only `inv-low` on the CPU during `inv-high`'s wait.

## Troubleshooting

- `mkqnximage` fails with `Host file 'startup-qemu-virt' not available`: install `com.qnx.qnx800.target.qemuvirt` (Step 2).
- Software Center login rejected: your password may contain a non-alphanumeric character. Use letters and digits only.
- The password prompt in a terminal was confusing: `docker/sdp.sh login` uses a macOS dialog for that reason.
- QEMU asserts in `gic_v3.c`, or QEMU refuses GICv2: you used `ACCEL=hvf`. Use the default, TCG.
- QEMU says it has no machine `virt-4.2`: that is `mkqnximage`'s own runner. Use `tools/qemu-run.sh`, which uses `virt-8.2`.
- The first `scp` after a run says "Permission denied": `tools/target.sh` retries up to four times. Wait and run it again.
- `kevscope` says it cannot decode a raw `.kev` file: run `traceprinter` first.
- `view` opens a tab but the trace does not load: Chrome may be waiting for you to allow ui.perfetto.dev to reach the local network. Or use "Open trace file" with the JSON.
- The guest is slow: it is emulated under TCG. Be patient with SSH logins.

## What to try next

- Trace your own program. Run `tracelogger -w` on the target, then `traceprinter`, then `kevscope view`.
- Add `trace_logf` calls around the code you care about and find them as markers.
- Run `kevscope blame` on a thread that is Ready, not Mutex-blocked, to see who it waited behind.
- Read `kevscope/timeline.py` and add a track you want.

## Clean up

Remove your stored credentials, if you have not already:

```bash
docker/sdp.sh logout
```

The SDP, the container's home and the image live in `~/qnx-linux`. Delete that folder to remove them. Traces are in `captures/` in the repository, which git ignores.

QNX is a trademark of BlackBerry Limited. kevscope is an independent project, not affiliated with or endorsed by BlackBerry.
