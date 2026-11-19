/*
 * rtdemo: a small QNX 8.0 real-time workload made to be traced.
 *
 * One process, five named threads pinned to one CPU so that they compete:
 *
 *   server   prio 20  MsgReceive() loop: answers requests, counts timer pulses
 *   sensor   prio 15  every 5 ms sends a short request  (MsgSend -> MsgReply)
 *   logger   prio  8  every 20 ms sends a longer request
 *   hog      prio 10  every 50 ms burns 3 ms of CPU (delays the logger)
 *   and, with -i, a classic priority inversion on a mutex that has no
 *   priority protocol, replayed every 100 ms:
 *     inv-low  (prio 5)  takes the lock, then wakes the other two
 *     inv-high (prio 18) wants the lock and blocks behind inv-low
 *     inv-mid  (prio 12) needs no lock at all, but it outranks inv-low, so it
 *                        runs for 10 ms while inv-high keeps waiting
 *   Add -p to give the mutex PTHREAD_PRIO_INHERIT: inv-low then inherits
 *   priority 18 while it holds the lock and inv-mid can no longer cut in.
 *
 * QNX-specific pieces used: channels and synchronous message passing
 * (ChannelCreate, ConnectAttach, MsgSend, MsgReceive, MsgReply), pulses
 * delivered by a POSIX timer (SIGEV_PULSE_INIT), ThreadCtl run masks, and
 * user trace events (trace_logf) that show up in the kernel event trace.
 *
 * Build (QNX SDP 8.0):  qcc -Vgcc_ntoaarch64le -O2 -Wall -o rtdemo rtdemo.c
 * Run on the target:    ./rtdemo [-t seconds] [-i [-p]]
 */
#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <semaphore.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <sys/neutrino.h>
#include <sys/trace.h>

#define PULSE_TICK   (_PULSE_CODE_MINAVAIL + 0)
#define PULSE_QUIT   (_PULSE_CODE_MINAVAIL + 1)
#define MSG_WORK     1

typedef struct {
    uint16_t type;          /* MSG_WORK */
    uint16_t pad;
    uint32_t work_us;       /* how long the server should compute */
} request_t;

typedef struct {
    uint32_t served;        /* requests served so far */
    uint32_t ticks;         /* timer pulses seen so far */
} reply_t;

typedef union {
    struct _pulse pulse;
    request_t req;
} inbound_t;

static int chid = -1;
static volatile int running = 1;
static pthread_mutex_t shared_lock;      /* protocol chosen in main(): none or inherit */
static sem_t go_high, go_mid;

static uint64_t now_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

static void burn_us(uint32_t us)
{
    uint64_t end = now_ns() + (uint64_t)us * 1000ull;
    volatile uint32_t x = 0;
    while (now_ns() < end) {
        x++;
    }
}

static void sleep_ms(unsigned ms)
{
    struct timespec ts = { ms / 1000, (long)(ms % 1000) * 1000000L };
    clock_nanosleep(CLOCK_MONOTONIC, 0, &ts, NULL);
}

static void setup_thread(const char *name, int prio)
{
    struct sched_param sp;
    uintptr_t runmask = 1;                /* CPU 0 only */

    memset(&sp, 0, sizeof sp);
    sp.sched_priority = prio;
    pthread_setname_np(pthread_self(), name);
    if (pthread_setschedparam(pthread_self(), SCHED_FIFO, &sp) != 0) {
        fprintf(stderr, "%s: cannot set priority %d\n", name, prio);
    }
    if (ThreadCtl(_NTO_TCTL_RUNMASK, (void *)runmask) == -1) {
        fprintf(stderr, "%s: ThreadCtl(RUNMASK): %s\n", name, strerror(errno));
    }
}

static void *server_thread(void *arg)
{
    inbound_t msg;
    reply_t rep = { 0, 0 };

    (void)arg;
    setup_thread("server", 20);
    for (;;) {
        rcvid_t rcvid = MsgReceive(chid, &msg, sizeof msg, NULL);
        if (rcvid == -1) {
            break;
        }
        if (rcvid == 0) {                 /* a pulse */
            if (msg.pulse.code == PULSE_TICK) {
                rep.ticks++;
            } else if (msg.pulse.code == PULSE_QUIT) {
                break;
            }
            continue;
        }
        if (msg.req.type == MSG_WORK) {
            burn_us(msg.req.work_us);
            rep.served++;
            MsgReply(rcvid, EOK, &rep, sizeof rep);
        } else {
            MsgError(rcvid, ENOSYS);
        }
    }
    return NULL;
}

struct client_cfg {
    const char *name;
    int prio;
    unsigned period_ms;
    uint32_t work_us;
};

static void *client_thread(void *arg)
{
    const struct client_cfg *cfg = arg;
    request_t req = { MSG_WORK, 0, cfg->work_us };
    reply_t rep;
    int coid;

    setup_thread(cfg->name, cfg->prio);
    coid = ConnectAttach(0, 0, chid, _NTO_SIDE_CHANNEL, 0);
    if (coid == -1) {
        perror("ConnectAttach");
        return NULL;
    }
    while (running) {
        if (MsgSend(coid, &req, sizeof req, &rep, sizeof rep) == -1) {
            break;
        }
        sleep_ms(cfg->period_ms);
    }
    ConnectDetach(coid);
    return NULL;
}

static void *hog_thread(void *arg)
{
    (void)arg;
    setup_thread("hog", 10);
    while (running) {
        burn_us(3000);
        sleep_ms(50);
    }
    return NULL;
}

static void *inv_low_thread(void *arg)
{
    (void)arg;
    setup_thread("inv-low", 5);
    while (running) {
        pthread_mutex_lock(&shared_lock);
        burn_us(1000);
        sem_post(&go_high);               /* inv-high preempts us and blocks on the lock */
        sem_post(&go_mid);                /* inv-mid preempts us unless we inherited prio 18 */
        burn_us(4000);                    /* rest of the critical section */
        pthread_mutex_unlock(&shared_lock);
        sleep_ms(100);
    }
    sem_post(&go_high);                   /* let the other two see running == 0 */
    sem_post(&go_mid);
    return NULL;
}

static void *inv_mid_thread(void *arg)
{
    (void)arg;
    setup_thread("inv-mid", 12);
    for (;;) {
        sem_wait(&go_mid);
        if (!running) {
            break;
        }
        burn_us(10000);                   /* unrelated work that never touches the lock */
    }
    return NULL;
}

static void *inv_high_thread(void *arg)
{
    (void)arg;
    setup_thread("inv-high", 18);
    for (;;) {
        sem_wait(&go_high);
        if (!running) {
            break;
        }
        pthread_mutex_lock(&shared_lock); /* held by inv-low right now */
        pthread_mutex_unlock(&shared_lock);
    }
    return NULL;
}

int main(int argc, char **argv)
{
    static const struct client_cfg sensor = { "sensor", 15, 5, 200 };
    static const struct client_cfg logger = { "logger", 8, 20, 1000 };
    pthread_t th[8];
    int nth = 0, seconds = 2, inversion = 0, inherit = 0, opt, coid;
    struct sigevent ev;
    struct itimerspec its;
    timer_t timer;

    while ((opt = getopt(argc, argv, "t:ip")) != -1) {
        if (opt == 't') {
            seconds = atoi(optarg);
        } else if (opt == 'i') {
            inversion = 1;
        } else if (opt == 'p') {
            inherit = 1;
        } else {
            fprintf(stderr, "usage: rtdemo [-t seconds] [-i [-p]]\n");
            return 2;
        }
    }

    chid = ChannelCreate(0);
    if (chid == -1) {
        perror("ChannelCreate");
        return 1;
    }
    coid = ConnectAttach(0, 0, chid, _NTO_SIDE_CHANNEL, 0);
    if (coid == -1) {
        perror("ConnectAttach");
        return 1;
    }

    {
        pthread_mutexattr_t ma;
        pthread_mutexattr_init(&ma);
        pthread_mutexattr_setprotocol(&ma, inherit ? PTHREAD_PRIO_INHERIT : PTHREAD_PRIO_NONE);
        pthread_mutex_init(&shared_lock, &ma);
        sem_init(&go_high, 0, 0);
        sem_init(&go_mid, 0, 0);
    }

    trace_logf(_NTO_TRACE_USERFIRST, "rtdemo start%s",
               !inversion ? "" : inherit ? " (inherit)" : " (inversion)");

    pthread_create(&th[nth++], NULL, server_thread, NULL);
    pthread_create(&th[nth++], NULL, client_thread, (void *)&sensor);
    pthread_create(&th[nth++], NULL, client_thread, (void *)&logger);
    pthread_create(&th[nth++], NULL, hog_thread, NULL);
    if (inversion) {
        pthread_create(&th[nth++], NULL, inv_high_thread, NULL);
        pthread_create(&th[nth++], NULL, inv_mid_thread, NULL);
        pthread_create(&th[nth++], NULL, inv_low_thread, NULL);
    }

    /* A 10 ms periodic timer that delivers a pulse to the server's channel. */
    SIGEV_PULSE_INIT(&ev, coid, 21, PULSE_TICK, 0);
    if (timer_create(CLOCK_MONOTONIC, &ev, &timer) == -1) {
        perror("timer_create");
        return 1;
    }
    its.it_value.tv_sec = 0;
    its.it_value.tv_nsec = 10 * 1000000L;
    its.it_interval = its.it_value;
    timer_settime(timer, 0, &its, NULL);

    sleep((unsigned)seconds);

    running = 0;
    timer_delete(timer);
    trace_logf(_NTO_TRACE_USERFIRST + 1, "rtdemo stop");
    sleep_ms(150);                        /* let the periodic threads finish a cycle */
    MsgSendPulse(coid, 21, PULSE_QUIT, 0);
    for (opt = 0; opt < nth; opt++) {
        pthread_join(th[opt], NULL);
    }
    printf("rtdemo: ran %d s on QNX, %d threads%s\n", seconds, nth,
           !inversion ? "" : inherit ? ", mutex with priority inheritance"
                                     : ", mutex without priority inheritance");
    return 0;
}
