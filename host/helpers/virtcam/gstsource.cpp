// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
#include "gstsource.h"

#include <fcntl.h>
#include <signal.h>
#include <sys/prctl.h>
#include <sys/wait.h>
#include <unistd.h>

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cstdio>

#include "uvcdesc.h"

namespace virtcam
{
namespace
{
constexpr int FRAME_FD = 3; // the child's end of the pipe
constexpr std::size_t JPEG_LIMIT = 16u << 20;
} // namespace

GstSource::GstSource(std::string pattern, std::uint32_t fourcc, std::uint16_t width,
                     std::uint16_t height, std::uint32_t interval, std::string launcher)
    : pattern_(std::move(pattern)), launcher_(std::move(launcher)), fourcc_(fourcc), width_(width),
      height_(height), interval_(interval)
{
    start();
}

GstSource::~GstSource()
{
    stop();
}

std::vector<std::string> GstSource::pipeline(const std::string &pattern, std::uint32_t fourcc,
                                             std::uint16_t width, std::uint16_t height,
                                             std::uint32_t interval, int fd)
{
    // the running time on the pattern shows the picture is live; MJPEG is jpegenc's baseline JPEG
    // from I420, as a camera's (4:2:0)
    std::vector<std::string> a = {"-q",
                                  "videotestsrc",
                                  "is-live=true",
                                  "pattern=" + pattern,
                                  "!",
                                  "video/x-raw,width=" + std::to_string(width) +
                                      ",height=" + std::to_string(height) + ",framerate=10000000/" +
                                      std::to_string(interval ? interval : 333333),
                                  "!",
                                  "timeoverlay",
                                  "halignment=right",
                                  "valignment=bottom",
                                  std::string("font-desc=Sans ") + (height >= 360 ? "24" : "12"),
                                  "!",
                                  "videoconvert",
                                  "!"};
    if (fourcc == FOURCC_MJPG)
        a.insert(a.end(),
                 {"video/x-raw,format=I420", "!", "jpegenc", "quality=85", "!", "image/jpeg", "!"});
    else
        a.insert(a.end(), {"video/x-raw,format=YUY2", "!"});
    a.insert(a.end(), {"fdsink", "fd=" + std::to_string(fd), "sync=false"});
    return a;
}

std::size_t GstSource::jpeg_end(const std::uint8_t *d, std::size_t size, std::size_t &begin)
{
    // entropy-coded data stuffs every 0xff with a 0x00, so FF D9 only ever ends the image
    std::size_t i = 0;
    while (i + 1 < size && !(d[i] == 0xff && d[i + 1] == 0xd8))
        i++;
    begin = i;
    for (std::size_t j = i + 2; j + 1 < size; j++)
        if (d[j] == 0xff && d[j + 1] == 0xd9)
            return j + 2;
    return 0;
}

void GstSource::start()
{
    int fds[2];
    if (::pipe2(fds, O_CLOEXEC) != 0) {
        std::perror("beanjar-virtcam: pipe");
        return;
    }
    auto args = pipeline(pattern_, fourcc_, width_, height_, interval_, FRAME_FD);
    std::vector<char *> argv;
    argv.push_back(launcher_.data());
    for (auto &s : args)
        argv.push_back(s.data());
    argv.push_back(nullptr);
    const pid_t parent = ::getpid();
    const pid_t pid = ::fork();
    if (pid == 0) {
        // the child: stdin from /dev/null, the frames to FRAME_FD, gone with this process
        ::prctl(PR_SET_PDEATHSIG, SIGTERM);
        if (::getppid() != parent)
            ::_exit(127);
        const int null = ::open("/dev/null", O_RDONLY);
        if (null >= 0)
            ::dup2(null, 0);
        ::dup2(fds[1], FRAME_FD);
        ::dup2(2, 1); // nothing of gst-launch on our stdout
        ::execvp(argv[0], argv.data());
        ::_exit(127);
    }
    ::close(fds[1]);
    if (pid < 0) {
        std::perror("beanjar-virtcam: fork");
        ::close(fds[0]);
        return;
    }
    child_ = pid;
    pipe_ = fds[0];
    const std::size_t frame_size = std::size_t(width_) * height_ * 2;
    reader_ = std::thread(&GstSource::read_frames, this, pipe_, fourcc_, frame_size);
}

void GstSource::stop()
{
    if (child_ > 0)
        ::kill(child_, SIGTERM);
    if (reader_.joinable())
        reader_.join(); // the pipe's end follows the child's
    if (pipe_ >= 0)
        ::close(pipe_);
    pipe_ = -1;
    if (child_ > 0) {
        int status;
        while (::waitpid(child_, &status, 0) < 0 && errno == EINTR) {
        }
    }
    child_ = -1;
    std::lock_guard lock(mutex_);
    latest_.clear();
    current_.clear();
    latest_seq_ = served_seq_ = 0;
}

void GstSource::read_frames(int fd, std::uint32_t fourcc, std::size_t frame_size)
{
    std::vector<std::uint8_t> buf;
    std::uint8_t chunk[65536];
    for (;;) {
        const ssize_t n = ::read(fd, chunk, sizeof chunk);
        if (n < 0 && errno == EINTR)
            continue;
        if (n <= 0)
            break;
        buf.insert(buf.end(), chunk, chunk + n);
        for (;;) {
            std::size_t begin = 0, end = 0;
            if (fourcc == FOURCC_MJPG) {
                end = jpeg_end(buf.data(), buf.size(), begin);
                if (!end) {
                    if (buf.size() > JPEG_LIMIT)
                        buf.clear();
                    break;
                }
            } else if (buf.size() >= frame_size) {
                end = frame_size;
            } else {
                break;
            }
            {
                std::lock_guard lock(mutex_);
                latest_.assign(buf.begin() + std::ptrdiff_t(begin),
                               buf.begin() + std::ptrdiff_t(end));
                latest_seq_++;
            }
            buf.erase(buf.begin(), buf.begin() + std::ptrdiff_t(end));
        }
    }
}

std::vector<usbipdcpp::VideoFormatInfo> GstSource::supported_formats() const
{
    return {current_format()};
}

usbipdcpp::VideoFormatInfo GstSource::current_format() const
{
    return {fourcc_,   width_,    height_,   std::uint32_t(max_frame_size()),
            interval_, interval_, interval_, 16};
}

bool GstSource::set_format(std::uint32_t fourcc, std::uint16_t width, std::uint16_t height,
                           std::uint32_t interval)
{
    if (fourcc != FOURCC_YUY2 && fourcc != FOURCC_MJPG)
        return false;
    if (fourcc == fourcc_ && width == width_ && height == height_ && interval == interval_ &&
        child_ > 0)
        return true; // usbipdcpp's commit sets it again each time
    stop();
    fourcc_ = fourcc;
    width_ = width;
    height_ = height;
    interval_ = interval;
    start();
    return true;
}

bool GstSource::get_frame(usbipdcpp::VideoFrame &frame)
{
    std::lock_guard lock(mutex_);
    const auto now = std::chrono::steady_clock::now();
    if (latest_seq_ != served_seq_) {
        current_.swap(latest_);
        served_seq_ = latest_seq_;
        served_at_ = now;
        samples_++;
    } else if (current_.empty()) {
        return false; // none yet: the handler sends empty packets and asks again
    } else if (now - served_at_ < std::chrono::nanoseconds(std::uint64_t(interval_) * 200)) {
        return false; // the next one is due from GStreamer's clock: empty packets until it comes
    } else {
        served_at_ = now; // GStreamer is two frame intervals late: the last one again
        repeats_++;
    }
    frame = {current_.data(), current_.size(), true};
    return true;
}

std::size_t GstSource::max_frame_size() const
{
    return std::size_t(width_) * height_ * 2; // YUY2; a JPEG of it is smaller
}

std::uint32_t GstSource::frame_interval() const
{
    // usbipdcpp's frame clock starts each frame at the call that starts it, and the pacing thread
    // calls once a millisecond, so at its own interval every frame would last a whole number of
    // milliseconds (34 for 33.3, 29.4 frames/s). It gets half: GStreamer's live clock, through
    // get_frame, then sets the rate
    return interval_ / 2;
}

} // namespace virtcam
