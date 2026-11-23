// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/*
 * Headless (no graphics): stands in for LG's compositor, /usr/bin/surface-manager, on a systemd
 * release. The stock surface-manager-daemon.service
 * runs surface-manager.sh, which execs this file, so the unit, its Type=notify and the hub's role
 * for /usr/bin/surface-manager (roles.d/com.webos.surfacemanager.role.json) are LG's as they are.
 *
 * What the boot waits for from the compositor:
 *  - its bus name. com.webos.surfacemanager is a static LS2 service (services.d, Exec
 *    /usr/bin/surface-manager): ls-hubd holds every call to it until it registers. W20L's
 *    settings-service.sh calls showSpinner (show true) on a new state's settings version check, in
 *    a `luna-send -n 1` that otherwise never returns, and settings-service is Type=notify: its
 * start timed out after 90 s, and multi-user.target (After shutdown.service, After
 * settings-service) with it. settings-service-recovery.sh calls showSpinner (show false). Those are
 * the only calls of W20L's units and scripts; showSpinner answers returnValue true, any other
 * method gets LS2's own "unknown method" error at once.
 *  - com.webos.coverUI, where the stock services.d file lists it among the compositor's names
 *    (W22O number1 does, and the hub's role for /usr/bin/surface-manager allows it there).
 *    number1's activity com.webos.app.cpcover.stolen calls getStatus, which ls-hubd holds while
 *    nobody owns the name: configurator-activity never ends, webos-rbd.target never starts.
 *    LG's compositor serves open, close and getStatus (StarfishExtendedLunaServiceAPI.qml)
 *    through the WebOSServices Service plugin: getStatus answers {"isRunning": <cover open>,
 *    "returnValue": true}, with "subscribed" for subscribe:true, and pushes on a change; open
 *    and close answer {"returnValue": true}. Headless shows no cover: getStatus is always
 *    false, a subscription is kept and never pushed to, open and close draw nothing. An LS2
 *    name needs a handle of its own (one LSRegister each).
 *  - READY=1 on $NOTIFY_SOCKET (the unit is Type=notify; this is its main process).
 *  - lsm-ready: `initctl emit lsm-ready`, which W20L's initctl turns into /tmp/lsm-ready
 *    (lsm-ready.path) and a READY=1 for surface-manager, as the Upstart stand-in emits it.
 * Then it stays up, as the compositor does.
 *
 * libluna-service2 and GLib are the firmware's, opened with dlopen: nothing of the firmware is
 * linked. The signatures are those of luna-service2 3.6 to 3.21 (tests/regress/tcg/ls2-compat.h).
 */
#include <dlfcn.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>

typedef struct LSError {
    int error_code;
    char *message;
    const char *file;
    int line;
    const char *func;
    void *padding;
    unsigned long magic;
} LSError;
typedef struct LSHandle LSHandle;
typedef struct LSMessage LSMessage;
typedef bool (*LSMethodFunction)(LSHandle *sh, LSMessage *msg, void *category_context);
typedef struct {
    const char *name;
    LSMethodFunction function;
    unsigned long flags;
} LSMethod;

static bool (*ls_error_init)(LSError *);
static void (*ls_error_free)(LSError *);
static bool (*ls_register)(const char *, LSHandle **, LSError *);
static bool (*ls_register_category)(LSHandle *, const char *, LSMethod *, void *, void *,
                                    LSError *);
static bool (*ls_gmain_attach)(LSHandle *, void *, LSError *);
static bool (*ls_message_reply)(LSHandle *, LSMessage *, const char *, LSError *);
static bool (*ls_message_is_subscription)(LSMessage *);
static bool (*ls_subscription_add)(LSHandle *, const char *, LSMessage *, LSError *);
static void *(*g_main_loop_new_)(void *, int);
static void (*g_main_loop_run_)(void *);

static void die(const char *what)
{
    fprintf(stderr, "surface-manager (emulator, headless): %s\n", what);
    exit(1);
}

static void *sym(void *lib, const char *name)
{
    void *p = dlsym(lib, name);
    if (!p)
        die(name);
    return p;
}

static void reply(LSHandle *sh, LSMessage *msg, const char *payload)
{
    LSError e;
    ls_error_init(&e);
    if (!ls_message_reply(sh, msg, payload, &e))
        ls_error_free(&e);
}

static bool reply_true(LSHandle *sh, LSMessage *msg, void *ctx)
{
    (void)ctx;
    reply(sh, msg, "{\"returnValue\":true}");
    return true;
}

static LSMethod methods[] = {
    {"showSpinner", reply_true, 0},
    {NULL, NULL, 0},
};

/* com.webos.coverUI/getStatus: no cover is ever open; a subscriber is kept, and never pushed to */
static bool cover_get_status(LSHandle *sh, LSMessage *msg, void *ctx)
{
    LSError e;
    bool subscribed;
    (void)ctx;

    if (!ls_message_is_subscription(msg)) {
        reply(sh, msg, "{\"isRunning\":false,\"returnValue\":true}");
        return true;
    }
    ls_error_init(&e);
    subscribed = ls_subscription_add(sh, "getStatus", msg, &e);
    if (!subscribed)
        ls_error_free(&e);
    reply(sh, msg,
          subscribed ? "{\"isRunning\":false,\"returnValue\":true,\"subscribed\":true}"
                     : "{\"isRunning\":false,\"returnValue\":true,\"subscribed\":false}");
    return true;
}

static LSMethod cover_methods[] = {
    {"open", reply_true, 0},
    {"close", reply_true, 0},
    {"getStatus", cover_get_status, 0},
    {NULL, NULL, 0},
};

/* Whether the stock services.d file names `name` among the compositor's (Name=a;b;...) */
static bool stock_serves(const char *name)
{
    FILE *f = fopen("/usr/share/luna-service2/services.d/com.webos.surfacemanager.service", "r");
    char line[4096];
    bool found = false;

    if (!f)
        return false;
    while (!found && fgets(line, sizeof line, f)) {
        char *tok, *save;
        if (strncmp(line, "Name=", 5))
            continue;
        for (tok = strtok_r(line + 5, "; \t\r\n", &save); tok && !found;
             tok = strtok_r(NULL, "; \t\r\n", &save))
            found = !strcmp(tok, name);
    }
    fclose(f);
    return found;
}

static void register_cover_ui(void *loop)
{
    LSHandle *sh;
    LSError e;

    if (!stock_serves("com.webos.coverUI"))
        return;
    ls_error_init(&e);
    if (!ls_register("com.webos.coverUI", &sh, &e) ||
        !ls_register_category(sh, "/", cover_methods, NULL, NULL, &e) ||
        !ls_gmain_attach(sh, loop, &e)) {
        /* logged, not fatal: com.webos.surfacemanager and READY=1 still matter more */
        fprintf(stderr, "surface-manager (emulator, headless): com.webos.coverUI: %s\n",
                e.message ? e.message : "LS2 registration failed");
        ls_error_free(&e);
        return;
    }
    fprintf(stderr, "surface-manager (emulator, headless): com.webos.coverUI up\n");
}

/* sd_notify(0, "READY=1") without libsystemd: one datagram to $NOTIFY_SOCKET ('@': abstract) */
static void notify_ready(void)
{
    const char *path = getenv("NOTIFY_SOCKET");
    struct sockaddr_un sa = {.sun_family = AF_UNIX};
    size_t n;
    int fd;

    if (!path || !*path || (n = strlen(path)) >= sizeof sa.sun_path)
        return;
    memcpy(sa.sun_path, path, n);
    if (sa.sun_path[0] == '@')
        sa.sun_path[0] = 0;
    fd = socket(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC, 0);
    if (fd < 0)
        return;
    if (sendto(fd, "READY=1", 7, 0, (struct sockaddr *)&sa,
               offsetof(struct sockaddr_un, sun_path) + n) < 0)
        perror("surface-manager (emulator, headless): READY=1");
    close(fd);
}

static void emit_lsm_ready(void)
{
    pid_t pid = fork();
    if (pid == 0) {
        execl("/usr/bin/initctl", "initctl", "emit", "--no-wait", "lsm-ready", (char *)NULL);
        _exit(127);
    }
    if (pid > 0)
        waitpid(pid, NULL, 0);
}

int main(int argc, char **argv)
{
    void *ls2, *glib, *loop;
    LSHandle *sh;
    LSError e;
    (void)argc;
    (void)argv;

    if (!(ls2 = dlopen("libluna-service2.so.3", RTLD_NOW)) ||
        !(glib = dlopen("libglib-2.0.so.0", RTLD_NOW)))
        die(dlerror());
    ls_error_init = sym(ls2, "LSErrorInit");
    ls_error_free = sym(ls2, "LSErrorFree");
    ls_register = sym(ls2, "LSRegister");
    ls_register_category = sym(ls2, "LSRegisterCategory");
    ls_gmain_attach = sym(ls2, "LSGmainAttach");
    ls_message_reply = sym(ls2, "LSMessageReply");
    ls_message_is_subscription = sym(ls2, "LSMessageIsSubscription");
    ls_subscription_add = sym(ls2, "LSSubscriptionAdd");
    g_main_loop_new_ = sym(glib, "g_main_loop_new");
    g_main_loop_run_ = sym(glib, "g_main_loop_run");

    loop = g_main_loop_new_(NULL, 0);
    ls_error_init(&e);
    if (!ls_register("com.webos.surfacemanager", &sh, &e) ||
        !ls_register_category(sh, "/", methods, NULL, NULL, &e) || !ls_gmain_attach(sh, loop, &e))
        die(e.message ? e.message : "LS2 registration failed");
    register_cover_ui(loop);
    notify_ready();
    emit_lsm_ready();
    fprintf(stderr,
            "surface-manager (emulator, headless): com.webos.surfacemanager up, lsm-ready\n");
    g_main_loop_run_(loop);
    return 0;
}
