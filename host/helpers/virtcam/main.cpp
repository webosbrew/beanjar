// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
// beanjar-virtcam: a virtual USB webcam (a Logitech C920 showing GStreamer's test pattern) for a
// QEMU usb-redir device. `beanjar io usb attach NAME --camera` runs it (host/core/beanjar/devices/usb.py).
//
//   beanjar-virtcam --to HOST:PORT | --unix PATH [--pattern NAME] [--stats SECONDS] [--verbose]
//
// It connects to the usb-redir chardev (a TCP port, as usbredirect takes it, or the unix socket
// itself), serves the camera until the connection closes or SIGINT, SIGTERM or SIGHUP (which
// unplug it first), and prints its stream counters to stderr every --stats seconds and at the end.
#include <netdb.h>
#include <signal.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include <spdlog/spdlog.h>

#include <cctype>
#include <cerrno>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <cstring>
#include <memory>
#include <mutex>
#include <string>
#include <thread>

#include "camera.h"
#include "gstsource.h"
#include "redir.h"

namespace
{

virtcam::Front *front_for_signals;

void on_signal(int)
{
    if (front_for_signals)
        front_for_signals->stop();
}

int usage()
{
    std::fprintf(stderr, "usage: beanjar-virtcam --to HOST:PORT | --unix PATH [--pattern NAME] "
                         "[--stats SECONDS] [--verbose]\n");
    return 2;
}

int connect_tcp(const std::string &spec)
{
    const auto colon = spec.rfind(':');
    if (colon == std::string::npos)
        return -1;
    std::string host = spec.substr(0, colon), port = spec.substr(colon + 1);
    if (host.size() > 2 && host.front() == '[' && host.back() == ']')
        host = host.substr(1, host.size() - 2);
    addrinfo hints{}, *res = nullptr;
    hints.ai_socktype = SOCK_STREAM;
    if (getaddrinfo(host.c_str(), port.c_str(), &hints, &res) != 0)
        return -1;
    int fd = -1;
    for (auto *a = res; a; a = a->ai_next) {
        fd = socket(a->ai_family, a->ai_socktype | SOCK_CLOEXEC, a->ai_protocol);
        if (fd >= 0 && connect(fd, a->ai_addr, a->ai_addrlen) == 0)
            break;
        if (fd >= 0)
            close(fd);
        fd = -1;
    }
    freeaddrinfo(res);
    return fd;
}

int connect_unix(const std::string &path)
{
    sockaddr_un a{};
    a.sun_family = AF_UNIX;
    if (path.size() >= sizeof a.sun_path)
        return -1;
    std::memcpy(a.sun_path, path.c_str(), path.size() + 1);
    const int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd >= 0 && connect(fd, reinterpret_cast<sockaddr *>(&a), sizeof a) != 0) {
        close(fd);
        return -1;
    }
    return fd;
}

// One line of counters: the stream's format, then over the last interval (since the previous
// line) and in total: frames sent (EOF packets) and their rate, packets, late wake-ups, skipped
// and dropped packets; with the GStreamer child's new and repeated frames.
struct Reporter {
    const virtcam::Front &front;
    const virtcam::Camera &camera;
    const virtcam::GstSource *source;
    virtcam::Stats last{};
    double last_at = 0;

    void print(double now, const char *when)
    {
        const auto s = front.stats();
        const auto c = camera.committed();
        const double span = now - last_at;
        const auto d = [&](std::uint64_t a, std::uint64_t b) {
            return (unsigned long long)(a - b);
        };
        std::fprintf(
            stderr,
            "beanjar-virtcam: %s %.1f s: format %u frame %u at %.2f fps; last %.1f s: frames %llu "
            "(%.1f/s), packets %llu, late wake-ups %llu, skipped %llu, dropped %llu; total: "
            "frames %llu, packets %llu (%llu with data, %.1f MB), late wake-ups %llu, skipped "
            "%llu, dropped %llu, streams %llu; GStreamer frames %llu, repeated %llu\n",
            when, now, c.format, c.frame, c.interval ? 1e7 / c.interval : 0.0, span,
            d(s.frames, last.frames), span > 0 ? double(s.frames - last.frames) / span : 0.0,
            d(s.packets, last.packets), d(s.late, last.late), d(s.skipped, last.skipped),
            d(s.dropped, last.dropped), (unsigned long long)s.frames, (unsigned long long)s.packets,
            (unsigned long long)s.data_packets, double(s.bytes) / 1e6, (unsigned long long)s.late,
            (unsigned long long)s.skipped, (unsigned long long)s.dropped,
            (unsigned long long)s.streams, (unsigned long long)(source ? source->samples() : 0),
            (unsigned long long)(source ? source->repeats() : 0));
        last = s;
        last_at = now;
    }
};

} // namespace

int main(int argc, char **argv)
{
    std::string to, unix_path, pattern = "smpte";
    double stats_every = 0;
    bool verbose = false;
    for (int i = 1; i < argc; i++) {
        const std::string a = argv[i];
        auto value = [&]() -> const char * { return i + 1 < argc ? argv[++i] : nullptr; };
        const char *v = nullptr;
        if (a == "--to" && (v = value()))
            to = v;
        else if (a == "--unix" && (v = value()))
            unix_path = v;
        else if (a == "--pattern" && (v = value()))
            pattern = v;
        else if (a == "--stats" && (v = value()))
            stats_every = std::atof(v);
        else if (a == "--verbose")
            verbose = true;
        else
            return usage();
    }
    if (to.empty() == unix_path.empty())
        return usage();
    for (char ch : pattern)
        if (!std::isalnum(static_cast<unsigned char>(ch)) && ch != '-')
            return usage();
    spdlog::set_level(verbose ? spdlog::level::debug : spdlog::level::off);

    const int fd = to.empty() ? connect_unix(unix_path) : connect_tcp(to);
    if (fd < 0) {
        std::fprintf(stderr, "beanjar-virtcam: cannot connect to %s: %s\n",
                     to.empty() ? unix_path.c_str() : to.c_str(), std::strerror(errno));
        return 1;
    }

    // The source starts in the camera's default format (YUY2 640x480 at 30 fps for the C920), so
    // the first frames are ready when the guest first commits it.
    auto source_owned =
        std::make_unique<virtcam::GstSource>(pattern, virtcam::FOURCC_YUY2, 640, 480, 333333);
    virtcam::GstSource *source = source_owned.get();
    virtcam::Camera camera(std::move(source_owned));
    {
        const auto def = virtcam::default_probe(camera.config());
        const auto *fmt = camera.config().format(def.format);
        const auto *frame = fmt ? fmt->frame(def.frame) : nullptr;
        if (fmt && frame)
            source->set_format(fmt->fourcc, frame->width, frame->height, def.interval);
    }
    virtcam::Front front(fd, camera);
    front_for_signals = &front;
    struct sigaction sa{};
    sa.sa_handler = on_signal;
    sigemptyset(&sa.sa_mask);
    for (int s : {SIGINT, SIGTERM, SIGHUP})
        sigaction(s, &sa, nullptr);

    const auto start = std::chrono::steady_clock::now();
    auto elapsed = [&] {
        return std::chrono::duration<double>(std::chrono::steady_clock::now() - start).count();
    };
    std::mutex m;
    std::condition_variable cv;
    bool done = false;
    Reporter report{front, camera, source};
    std::thread reporter;
    if (stats_every > 0)
        reporter = std::thread([&] {
            std::unique_lock lock(m);
            while (!cv.wait_for(lock, std::chrono::duration<double>(stats_every),
                                [&] { return done; }))
                report.print(elapsed(), "after");
        });
    const int rc = front.run();
    {
        std::lock_guard lock(m);
        done = true;
    }
    cv.notify_all();
    if (reporter.joinable())
        reporter.join();
    front_for_signals = nullptr;
    report.print(elapsed(), "ended after");
    close(fd);
    return rc;
}
