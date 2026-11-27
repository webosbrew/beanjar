// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
// beanjar-virtpad: a virtual wired Xbox 360 controller (045e:028e) for a QEMU usb-redir device.
// `beanjar io usb gamepad attach NAME` starts it on the VM's host (host/core/beanjar/devices/gamepad.py).
//
//   beanjar-virtpad --unix PATH --control PATH [--retry SECONDS]
//
// It connects to the usb-redir chardev's unix socket and serves the pad. When the connection ends
// (the guest restarted, so QEMU did) it connects again, for up to --retry seconds (default 180);
// SIGINT, SIGTERM or SIGHUP unplug the pad (device_disconnect) and end it.
//
// --control is a unix socket it listens on for one-line commands, each answered with one line:
//   press HOLD_MS CONTROL...  press and release each control in turn, each held HOLD_MS and
//                             followed by as long released; "ok REPORTS POLLED" once done, REPORTS
//                             the input reports the guest was sent, POLLED 1 if its driver polls
//                             the pad (its input device is open), else 0 (nothing was sent)
//   state                     "ok {JSON}": connected, configured, polled, reports, outputs, led,
//                             rumble
// An error is "error TEXT". Presses queue behind one another.
#include <poll.h>
#include <signal.h>
#include <sys/signalfd.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#include <cerrno>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <memory>
#include <sstream>
#include <string>
#include <vector>

#include "pad.h"
#include "redir.h"

namespace
{

using Clock = std::chrono::steady_clock;

int usage()
{
    std::fprintf(stderr, "usage: beanjar-virtpad --unix PATH --control PATH [--retry SECONDS]\n");
    return 2;
}

bool fill_address(sockaddr_un &a, const std::string &path)
{
    a = {};
    a.sun_family = AF_UNIX;
    if (path.size() >= sizeof a.sun_path)
        return false;
    std::memcpy(a.sun_path, path.c_str(), path.size() + 1);
    return true;
}

int connect_unix(const std::string &path)
{
    sockaddr_un a;
    if (!fill_address(a, path))
        return -1;
    const int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd >= 0 && connect(fd, reinterpret_cast<sockaddr *>(&a), sizeof a) != 0) {
        close(fd);
        return -1;
    }
    return fd;
}

int listen_unix(const std::string &path)
{
    sockaddr_un a;
    if (!fill_address(a, path))
        return -1;
    unlink(path.c_str());
    const int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
    const mode_t old = umask(077);
    const bool ok = fd >= 0 && bind(fd, reinterpret_cast<sockaddr *>(&a), sizeof a) == 0 &&
                    listen(fd, 8) == 0;
    umask(old);
    if (!ok) {
        if (fd >= 0)
            close(fd);
        return -1;
    }
    return fd;
}

struct Step {
    Clock::duration after; // since the previous step
    virtpad::State state;
};

struct Job {
    int client = -1;
    std::vector<Step> steps;
    std::size_t next = 0;
    Clock::time_point due;
    std::uint64_t reports0 = 0;
    bool polled = true;
};

struct Client {
    int fd;
    std::string in;
};

void reply(int fd, const std::string &line)
{
    const std::string out = line + "\n";
    [[maybe_unused]] auto n = send(fd, out.data(), out.size(), MSG_NOSIGNAL);
}

class Server
{
  public:
    Server(std::string socket_path, std::string control_path, double retry)
        : socket_path_(std::move(socket_path)), control_path_(std::move(control_path)),
          retry_(std::chrono::duration_cast<Clock::duration>(std::chrono::duration<double>(retry)))
    {
    }
    ~Server()
    {
        for (auto &c : clients_)
            close(c.fd);
        if (listen_fd_ >= 0) {
            close(listen_fd_);
            unlink(control_path_.c_str());
        }
        drop_front();
    }

    int run()
    {
        sigset_t set;
        sigemptyset(&set);
        for (int s : {SIGINT, SIGTERM, SIGHUP})
            sigaddset(&set, s);
        sigprocmask(SIG_BLOCK, &set, nullptr);
        const int sig_fd = signalfd(-1, &set, SFD_CLOEXEC | SFD_NONBLOCK);
        listen_fd_ = listen_unix(control_path_);
        if (listen_fd_ < 0) {
            std::fprintf(stderr, "beanjar-virtpad: cannot listen on %s: %s\n",
                         control_path_.c_str(), std::strerror(errno));
            return 1;
        }
        auto lost_at = Clock::now();
        bool ever = false;
        for (;;) {
            if (!front_) {
                const int fd = connect_unix(socket_path_);
                if (fd >= 0) {
                    front_ = std::make_unique<virtpad::Front>(fd, pad_);
                    ever = true;
                } else if (Clock::now() - lost_at > retry_) {
                    std::fprintf(stderr, "beanjar-virtpad: %s %s for %.0f s; ending\n",
                                 socket_path_.c_str(), ever ? "gone" : "not reachable",
                                 std::chrono::duration<double>(retry_).count());
                    return ever ? 0 : 1;
                }
            }
            std::vector<pollfd> fds{{sig_fd, POLLIN, 0}, {listen_fd_, POLLIN, 0}};
            if (front_)
                fds.push_back({front_->fd(), short(POLLIN | (front_->want_write() ? POLLOUT : 0)),
                               0});
            const std::size_t first_client = fds.size();
            for (auto &c : clients_)
                fds.push_back({c.fd, POLLIN, 0});
            int timeout = front_ ? -1 : 500;
            if (!jobs_.empty() && front_) {
                const auto left = std::chrono::duration_cast<std::chrono::milliseconds>(
                                      jobs_.front().due - Clock::now())
                                      .count() +
                                  1;
                timeout = int(std::max<long long>(0, left));
            }
            if (poll(fds.data(), fds.size(), timeout) < 0 && errno != EINTR)
                return 1;
            if (fds[0].revents & POLLIN) {
                if (front_)
                    front_->disconnect(1000);
                std::fprintf(stderr, "beanjar-virtpad: unplugged\n");
                return 0;
            }
            if (fds[1].revents & POLLIN)
                accept_clients();
            if (front_ && (fds[2].revents & (POLLIN | POLLOUT | POLLHUP | POLLERR))) {
                if (!front_->io()) {
                    std::fprintf(stderr, "beanjar-virtpad: %s\n",
                                 front_->failed() ? "the connection failed"
                                                  : "QEMU closed the connection");
                    drop_front();
                    lost_at = Clock::now();
                }
            }
            for (std::size_t i = first_client; i < fds.size(); i++)
                if (fds[i].revents)
                    read_client(fds[i].fd);
            clients_erase_closed();
            step_jobs();
        }
    }

  private:
    void drop_front()
    {
        if (!front_)
            return;
        const int fd = front_->fd();
        reports_ += front_->stats().reports;
        front_.reset();
        close(fd);
        while (!jobs_.empty()) {
            reply(jobs_.front().client, "error the VM's QEMU closed the pad's connection");
            jobs_.pop_front();
        }
    }

    void accept_clients()
    {
        for (;;) {
            const int fd = accept4(listen_fd_, nullptr, nullptr, SOCK_CLOEXEC);
            if (fd < 0)
                return;
            clients_.push_back({fd, {}});
        }
    }

    void read_client(int fd)
    {
        for (auto &c : clients_) {
            if (c.fd != fd)
                continue;
            char buf[512];
            const ssize_t n = recv(fd, buf, sizeof buf, MSG_DONTWAIT);
            if (n <= 0) {
                if (n == 0 || (errno != EAGAIN && errno != EINTR))
                    c.in = "\x01closed";
                return;
            }
            c.in.append(buf, std::size_t(n));
            std::size_t nl;
            while ((nl = c.in.find('\n')) != std::string::npos) {
                const std::string line = c.in.substr(0, nl);
                c.in.erase(0, nl + 1);
                command(fd, line);
            }
            if (c.in.size() > 4096)
                c.in = "\x01closed";
            return;
        }
    }

    void clients_erase_closed()
    {
        for (auto it = clients_.begin(); it != clients_.end();) {
            if (it->in == "\x01closed") {
                const int fd = it->fd;
                for (auto j = jobs_.begin(); j != jobs_.end();)
                    j = j->client == fd && j != jobs_.begin() ? jobs_.erase(j) : std::next(j);
                if (!jobs_.empty() && jobs_.front().client == fd)
                    jobs_.front().client = -1; // finishes, nobody to tell
                close(fd);
                it = clients_.erase(it);
            } else {
                ++it;
            }
        }
    }

    std::string state_json() const
    {
        std::ostringstream o;
        const bool c = front_ && front_->connected();
        o << "{\"connected\": " << (c ? "true" : "false")
          << ", \"configured\": " << (c && front_->configured() ? "true" : "false")
          << ", \"polled\": " << (c && front_->polled() ? "true" : "false")
          << ", \"reports\": " << reports_ + (front_ ? front_->stats().reports : 0)
          << ", \"outputs\": " << (front_ ? front_->stats().outputs : 0)
          << ", \"led\": " << int(pad_.led()) << ", \"rumble\": [" << int(pad_.rumble_big())
          << ", " << int(pad_.rumble_small()) << "]}";
        return o.str();
    }

    void command(int fd, const std::string &line)
    {
        std::istringstream in(line);
        std::string verb;
        in >> verb;
        if (verb == "state") {
            reply(fd, "ok " + state_json());
            return;
        }
        if (verb != "press") {
            reply(fd, "error unknown command: " + verb);
            return;
        }
        long hold_ms = -1;
        in >> hold_ms;
        if (!in || hold_ms < 1 || hold_ms > 60000) {
            reply(fd, "error press HOLD_MS CONTROL...: HOLD_MS from 1 to 60000");
            return;
        }
        std::vector<virtpad::Control> controls;
        std::string name;
        while (in >> name) {
            const auto c = virtpad::control_named(name);
            if (!c) {
                reply(fd, "error no such control: " + name);
                return;
            }
            controls.push_back(*c);
        }
        if (controls.empty()) {
            reply(fd, "error press HOLD_MS CONTROL...: no control");
            return;
        }
        if (!front_ || !front_->connected()) {
            reply(fd, "error the pad is not connected to the VM's QEMU");
            return;
        }
        Job job;
        job.client = fd;
        const Clock::duration hold = std::chrono::milliseconds(hold_ms);
        virtpad::State s{};
        for (std::size_t i = 0; i < controls.size(); i++) {
            s.press(controls[i]);
            job.steps.push_back({i ? hold : Clock::duration::zero(), s});
            s.release(controls[i]);
            job.steps.push_back({hold, s});
        }
        // the gap after the last release, so a following press is a press of its own
        job.steps.push_back({hold, s});
        const bool idle = jobs_.empty();
        jobs_.push_back(std::move(job));
        if (idle)
            start_job();
    }

    void start_job()
    {
        if (jobs_.empty())
            return;
        auto &j = jobs_.front();
        j.due = Clock::now() + j.steps[0].after;
        j.reports0 = front_ ? front_->stats().reports : 0;
        j.polled = true;
    }

    void step_jobs()
    {
        while (!jobs_.empty() && front_) {
            auto &j = jobs_.front();
            if (Clock::now() < j.due)
                return;
            j.polled &= front_->polled();
            front_->set_state(j.steps[j.next].state);
            if (++j.next < j.steps.size()) {
                j.due += j.steps[j.next].after;
                continue;
            }
            if (j.client >= 0)
                reply(j.client, "ok " + std::to_string(front_->stats().reports - j.reports0) + " " +
                                    (j.polled ? "1" : "0"));
            jobs_.pop_front();
            start_job();
        }
    }

    std::string socket_path_, control_path_;
    Clock::duration retry_;
    virtpad::Pad pad_;
    std::unique_ptr<virtpad::Front> front_;
    int listen_fd_ = -1;
    std::vector<Client> clients_;
    std::deque<Job> jobs_;
    std::uint64_t reports_ = 0; // of the connections before this one
};

} // namespace

int main(int argc, char **argv)
{
    std::string unix_path, control;
    double retry = 180;
    for (int i = 1; i < argc; i++) {
        const std::string a = argv[i];
        auto value = [&]() -> const char * { return i + 1 < argc ? argv[++i] : nullptr; };
        const char *v = nullptr;
        if (a == "--unix" && (v = value()))
            unix_path = v;
        else if (a == "--control" && (v = value()))
            control = v;
        else if (a == "--retry" && (v = value()))
            retry = std::atof(v);
        else
            return usage();
    }
    if (unix_path.empty() || control.empty() || retry < 0)
        return usage();
    Server server(unix_path, control, retry);
    return server.run();
}
