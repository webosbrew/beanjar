// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* QEMU's absolute tablet -> LG builtin Magic Remote, in the arm64 initramfs. */
#include <errno.h>
#include <fcntl.h>
#include <glob.h>
#include <linux/input.h>
#include <linux/uinput.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <time.h>
#include <unistd.h>

static volatile sig_atomic_t stopped;
static int out, width = 1920, height = 1080;
static int mrcu_wakeup;
/* W21A lginput2's custom-event table, cursor-on (index 4). Its sender
 * emits a press/release pulse followed by SYN_REPORT before motion. */
#define MRCU_CURSOR_ON 0x4ae
static struct input_absinfo axes[2];
static unsigned char buttons[3];
/* The pointer's position in output pixels (have_pos once the tablet is read), and our own M-RCU's
 * evdev node, through which position() makes the kernel forget the last value it passed. */
static int pos[2], have_pos, self = -1;
static void stop(int sig)
{
    (void)sig;
    stopped = 1;
}
static void fail(const char *what)
{
    perror(what);
    exit(1);
}
static void emit(unsigned short type, unsigned short code, int value)
{
    struct input_event ev = {.type = type, .code = code, .value = value};
    ssize_t n;
    do {
        n = write(out, &ev, sizeof(ev));
    } while (n < 0 && errno == EINTR);
    if (n != sizeof(ev))
        fail("pointer write");
}
static void release_buttons(void)
{
    for (int i = 0; i < 3; i++)
        if (buttons[i]) {
            emit(EV_KEY, BTN_LEFT + i, 0);
            buttons[i] = 0;
        }
    emit(EV_SYN, SYN_REPORT, 0);
}
static void cursor_on(void)
{
    if (!mrcu_wakeup)
        return;
    emit(EV_KEY, MRCU_CURSOR_ON, 1);
    emit(EV_KEY, MRCU_CURSOR_ON, 0);
    emit(EV_SYN, SYN_REPORT, 0);
}
/* libim (23.21.20 K8HP, IM_INPUT_SEND_InputEventProcess) passes a motion on only once 16 ms (its
 * 60 fps limit) have gathered since the event before; a motion held back is sent later only after
 * one has gone through. Until then a press reports libim's last sent position, (0,0) after boot, so
 * a click right after cursor-on landed at (0,0) on the first use after boot. A gap of more than
 * the limit after cursor-on, and before a press, lets the position through. */
#define WAKE_GAP_US 20000
static void wake_gap(void)
{
    struct timespec t = {.tv_nsec = WAKE_GAP_US * 1000L};
    while (nanosleep(&t, &t) < 0 && errno == EINTR && !stopped)
        ;
}
/* Our M-RCU's /dev/input/eventN, opened once (wakeup mode): its EVIOCSABS lets position() re-send
 * an unchanged value. Events are masked off for this reader, which never reads. */
static void open_self(void)
{
    char sys[64] = {0}, pattern[128];
    glob_t paths = {0};
    if (self >= 0 || ioctl(out, UI_GET_SYSNAME(sizeof(sys)), sys) < 0)
        return;
    snprintf(pattern, sizeof(pattern), "/sys/devices/virtual/input/%s/event*", sys);
    if (glob(pattern, 0, NULL, &paths) == 0 && paths.gl_pathc) {
        char node[64];
        snprintf(node, sizeof(node), "/dev/input/%s", strrchr(paths.gl_pathv[0], '/') + 1);
        self = open(node, O_RDONLY | O_NONBLOCK | O_CLOEXEC);
#ifdef EVIOCSMASK
        if (self >= 0) {
            static const unsigned short types[] = {EV_SYN, EV_KEY, EV_REL, EV_ABS};
            for (size_t i = 0; i < sizeof(types) / sizeof(types[0]); i++) {
                struct input_mask mask = {.type = types[i]};
                ioctl(self, EVIOCSMASK, &mask);
            }
        }
#endif
    }
    globfree(&paths);
}
/* Both axes, whether or not they changed. libim resets its cursor position when cursor-on shows
 * the cursor, and the input core drops an absolute value equal to the last one it passed, so after
 * cursor-on the position must be re-sent in full: the kernel's last value is set out of range
 * first (EVIOCSABS on our own node: no event), so the real one goes through. */
static void position(void)
{
    if (!have_pos)
        return;
    open_self();
    for (int a = 0; a < 2; a++) {
        struct input_absinfo forget = {.value = -1, .maximum = (a ? height : width) - 1};
        if (self >= 0)
            ioctl(self, EVIOCSABS(a), &forget);
        emit(EV_ABS, a, pos[a]);
    }
}
/* A tablet value as an output pixel. QEMU maps display pixel p to p * range / size (the display's
 * width or height onto the whole axis), so p comes back as that times size / range, rounded; with
 * size - 1 instead, pixels far from the origin came out one short (750 of 1080 as 749). */
static int scale(int axis, int value)
{
    int min = axes[axis].minimum, max = axes[axis].maximum;
    if (value < min)
        value = min;
    if (value > max)
        value = max;
    int size = axis ? height : width;
    int64_t range = (int64_t)max - min;
    int p = (int)((((int64_t)value - min) * size + range / 2) / range);
    return p < size ? p : size - 1;
}
static int tablet(void)
{
    glob_t paths = {0};
    int found = -1;
    if (glob("/dev/input/event*", 0, NULL, &paths) == 0) {
        for (size_t i = 0; i < paths.gl_pathc; i++) {
            char name[256] = {0};
            int fd = open(paths.gl_pathv[i], O_RDONLY | O_NONBLOCK | O_CLOEXEC);
            if (fd < 0)
                continue;
            if (ioctl(fd, EVIOCGNAME(sizeof(name)), name) >= 0 &&
                !strcmp(name, "QEMU Virtio Tablet")) {
                for (int a = 0; a < 2; a++)
                    if (ioctl(fd, EVIOCGABS(a), &axes[a]) < 0 || axes[a].maximum <= axes[a].minimum)
                        fail("tablet axis");
                if (ioctl(fd, EVIOCGRAB, 1) < 0)
                    fail("grab tablet");
                for (int a = 0; a < 2; a++)
                    pos[a] = scale(a, axes[a].value);
                have_pos = 1;
                found = fd;
                break;
            }
            close(fd);
        }
    }
    globfree(&paths);
    return found;
}
static void reconcile(int in)
{
    unsigned char keys[(BTN_MIDDLE + 8) / 8] = {0};
    release_buttons();
    if (ioctl(in, EVIOCGKEY(sizeof(keys)), keys) >= 0) {
        for (int i = 0; i < 3; i++) {
            int code = BTN_LEFT + i;
            if (keys[code / 8] & (1u << (code % 8))) {
                emit(EV_KEY, code, 1);
                buttons[i] = 1;
            }
        }
    }
    for (int a = 0; a < 2; a++) {
        struct input_absinfo info;
        if (ioctl(in, EVIOCGABS(a), &info) >= 0)
            pos[a] = scale(a, info.value);
    }
    have_pos = 1;
    cursor_on();
    position();
    emit(EV_SYN, SYN_REPORT, 0);
}
int main(int argc, char **argv)
{
    char tail;
    if (argc > 3 || (argc == 3 && strcmp(argv[2], "--mrcu-wakeup")) ||
        (argc >= 2 && (sscanf(argv[1], "%dx%d%c", &width, &height, &tail) != 2 || width < 2 ||
                       height < 2 || width > 16384 || height > 16384))) {
        fprintf(stderr, "usage: virtio-pointer [WIDTHxHEIGHT [--mrcu-wakeup]]\n");
        return 1;
    }
    mrcu_wakeup = argc == 3;
    out = open("/dev/uinput", O_WRONLY | O_CLOEXEC);
    if (out < 0)
        fail("open uinput");
    if (ioctl(out, UI_SET_EVBIT, EV_KEY) < 0 || ioctl(out, UI_SET_EVBIT, EV_REL) < 0 ||
        ioctl(out, UI_SET_RELBIT, REL_WHEEL) < 0 || ioctl(out, UI_SET_EVBIT, EV_ABS) < 0)
        fail("pointer capabilities");
    for (int i = BTN_LEFT; i <= BTN_MIDDLE; i++)
        if (ioctl(out, UI_SET_KEYBIT, i) < 0)
            fail("pointer button");
    if (mrcu_wakeup && ioctl(out, UI_SET_KEYBIT, MRCU_CURSOR_ON) < 0)
        fail("M-RCU cursor-on capability");
    for (int a = 0; a < 2; a++) {
        struct uinput_abs_setup abs = {.code = a, .absinfo = {.maximum = (a ? height : width) - 1}};
        if (ioctl(out, UI_SET_ABSBIT, a) < 0 || ioctl(out, UI_ABS_SETUP, &abs) < 0)
            fail("pointer axis");
    }
    /* WT1H lginput2 creates [0..2]. libim accepts indices [0..4]. */
    struct uinput_setup setup = {.id = {.bustype = BUS_USB, .version = 4},
                                 .name = "LGE M-RCU - Builtin [3]"};
    if (ioctl(out, UI_DEV_SETUP, &setup) < 0 || ioctl(out, UI_DEV_CREATE) < 0)
        fail("create M-RCU");
    pid_t pid = fork();
    if (pid < 0)
        fail("fork");
    if (pid > 0) {
        close(out);
        return 0;
    }
    if (setsid() < 0)
        fail("setsid");
    signal(SIGTERM, stop);
    signal(SIGINT, stop);
    int in = -1, dropped = 0, moved = 0, woke = 0;
    while (!stopped) {
        if (in < 0) {
            in = tablet();
            if (in < 0) {
                sleep(1);
                continue;
            }
        }
        struct pollfd p = {.fd = in, .events = POLLIN};
        int ready = poll(&p, 1, 1000);
        if (ready < 0 && errno != EINTR)
            fail("poll tablet");
        if (ready <= 0)
            continue;
        struct input_event ev;
        ssize_t n = read(in, &ev, sizeof(ev));
        if (n < 0 && (errno == EAGAIN || errno == EINTR))
            continue;
        if (n != sizeof(ev)) {
            release_buttons();
            close(in);
            in = -1;
            dropped = moved = woke = 0;
            continue;
        }
        if (ev.type == EV_SYN && ev.code == SYN_DROPPED) {
            dropped = 1;
            continue;
        }
        if (dropped) {
            if (ev.type == EV_SYN && ev.code == SYN_REPORT) {
                reconcile(in);
                dropped = moved = woke = 0;
            }
            continue;
        }
        int press =
            ev.type == EV_KEY && ev.code >= BTN_LEFT && ev.code <= BTN_MIDDLE && ev.value == 1;
        if (ev.type == EV_ABS && ev.code <= ABS_Y) {
            pos[ev.code] = scale(ev.code, ev.value);
            have_pos = 1;
            if (mrcu_wakeup)
                moved = 1; /* sent with the frame's first key, or at its end */
            else
                emit(EV_ABS, ev.code, pos[ev.code]);
            continue;
        }
        /* Wakeup mode: before a frame's motion or a button press, cursor-on and the whole
         * position. A click where the pointer already is carries no ABS (the input core drops a
         * repeated value), and once libim has hidden the cursor it would take the press at a
         * reset position: (0,0) to a native app. */
        if (mrcu_wakeup && (moved || press) && !woke) {
            cursor_on();
            wake_gap();
            position();
            /* the position in a frame of its own, and the press a gap later: libim drops the ABS
             * of a frame that has a key (any EV_KEY clears its queued motion) */
            emit(EV_SYN, SYN_REPORT, 0);
            if (press)
                wake_gap();
            woke = 1;
        } else if (moved) {
            emit(EV_ABS, ABS_X, pos[ABS_X]);
            emit(EV_ABS, ABS_Y, pos[ABS_Y]);
        }
        moved = 0;
        if (ev.type == EV_KEY && ev.code >= BTN_LEFT && ev.code <= BTN_MIDDLE) {
            emit(EV_KEY, ev.code, ev.value);
            buttons[ev.code - BTN_LEFT] = ev.value != 0;
        } else if ((ev.type == EV_REL && ev.code == REL_WHEEL) ||
                   (ev.type == EV_SYN && ev.code == SYN_REPORT))
            emit(ev.type, ev.code, ev.value);
        if (ev.type == EV_SYN && ev.code == SYN_REPORT)
            woke = 0;
    }
    release_buttons();
    if (in >= 0)
        close(in);
    ioctl(out, UI_DEV_DESTROY);
    close(out);
    return 0;
}
