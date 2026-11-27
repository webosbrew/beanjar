// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
#ifndef WEBOS_SHELL_H
#define WEBOS_SHELL_H
#include <stdint.h>
/* `beanjar shell` on the webos.shell virtio-serial port: shells and commands inside webOS, between
 * the host's relay (host/core/beanjar/access/guestshell.py, which serves RUN_DIR/shell.sock to any
 * number of clients and renumbers their sessions) and the initramfs's agent
 * (guest/initramfs/src/virtio-shell.c), which runs each session's command with webos-enter.
 * Clients speak the same frames to the relay.
 *
 * Both ways a stream of frames, each a header and len bytes (at most WEBOS_SHELL_MAX), all words
 * little-endian:
 *   magic    WEBOS_SHELL_MAGIC
 *   type     below
 *   session  the session the frame is about (0 for HELLO)
 *   len      the payload's bytes
 * Each side reads nothing but a HELLO until it has one (a peer that comes back may find the tail
 * of an older frame first), and a frame without the magic makes it look for one again.
 *
 *   HELLO    both ways, payload two words: WEBOS_SHELL_VERSION and a nonce. The host sends it when
 *            it connects, with a nonce of its own; the agent ends every session then and answers
 *            with the same nonce. The agent sends it with nonce 0 when it starts and when a host
 *            comes back after a hang-up of the port: its sessions are gone, and the host sends
 *            its HELLO again. The host takes sessions only after the answer to its own nonce.
 *   OPEN     host -> guest: struct webos_shell_open, the terminal type (term_len bytes, only
 *            with a terminal) and the command's words, each ended by a NUL; none: a login shell.
 *            With WEBOS_SHELL_TTY the session has a terminal of rows x cols, else stdin, stdout
 *            and stderr are pipes.
 *   STDIN    host -> guest: bytes for the command's stdin; len 0: its end (without a terminal)
 *   STDOUT   guest -> host: the command's output (with a terminal, everything it shows)
 *   STDERR   guest -> host: its stderr, without a terminal
 *   WINSIZE  host -> guest: two halfwords, rows and cols
 *   SIGNAL   host -> guest: one word, a signal (WEBOS_SHELL_SIG*) for the command's process group
 *   ACK      both ways: one word, bytes of STDIN (from the guest) or of STDOUT and STDERR (from
 *            the host) taken by their reader. Neither side sends more than WEBOS_SHELL_WINDOW
 *            bytes of a session's data that the other has not acknowledged.
 *   EXIT     guest -> host: one word, the command's status: its exit code, or 128 + its signal.
 *            After its last output; the session is over.
 *   CLOSE    host -> guest: the host gives the session up: the command's process group gets
 *            SIGHUP (and SIGKILL if it is still there 3 s later). guest -> host: the session ended
 *            without a status (it could not start, or the agent did not know it); the payload is
 *            the reason, text. The relay sends it to a client whose sessions the agent lost. */
#define WEBOS_SHELL_MAGIC   0x4c485357u /* "WSHL" */
#define WEBOS_SHELL_VERSION 1u
#define WEBOS_SHELL_MAX     65536u
#define WEBOS_SHELL_WINDOW  (256u << 10)
enum {
    WEBOS_SHELL_HELLO = 1,
    WEBOS_SHELL_OPEN,
    WEBOS_SHELL_STDIN,
    WEBOS_SHELL_STDOUT,
    WEBOS_SHELL_STDERR,
    WEBOS_SHELL_WINSIZE,
    WEBOS_SHELL_SIGNAL,
    WEBOS_SHELL_ACK,
    WEBOS_SHELL_EXIT,
    WEBOS_SHELL_CLOSE,
};
#define WEBOS_SHELL_TTY 1u
/* the signals a host may send, by their numbers on every POSIX host and in the guest */
#define WEBOS_SHELL_SIGHUP  1u
#define WEBOS_SHELL_SIGINT  2u
#define WEBOS_SHELL_SIGQUIT 3u
#define WEBOS_SHELL_SIGKILL 9u
#define WEBOS_SHELL_SIGTERM 15u
struct webos_shell_frame {
    uint32_t magic;
    uint16_t type, reserved;
    uint32_t session, len;
};
struct webos_shell_open {
    uint16_t flags, rows, cols, term_len;
};
_Static_assert(sizeof(struct webos_shell_frame) == 16, "shell wire header");
_Static_assert(sizeof(struct webos_shell_open) == 8, "shell wire open");
#endif
