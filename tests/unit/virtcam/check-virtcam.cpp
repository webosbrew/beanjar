// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
// The virtual camera's parts without a VM (host/helpers/virtcam): the descriptors read back and the
// probe negotiation, the pacing arithmetic, the payload header fix, the JPEG framing and the
// gst-launch child's restart and end, and the usbredir front end against a fake QEMU (a
// libusbredirparser in the guest role over a socket pair): enumeration, the probe and commit, an
// alternate setting, and a paced isochronous stream counted for half a second.
#include <dirent.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <usbredirparser.h>

#include <chrono>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <functional>
#include <map>
#include <memory>
#include <string>
#include <thread>
#include <vector>

#include "camera.h"
#include "gstsource.h"
#include "pacing.h"
#include "redir.h"
#include "uvcdesc.h"

using namespace virtcam;

static int failures = 0;
#define CHECK(cond)                                                                                \
    do {                                                                                           \
        if (!(cond)) {                                                                             \
            std::fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);                   \
            failures++;                                                                            \
        }                                                                                          \
    } while (0)

namespace
{

// Frames of a fixed size, always ready: the stream's timing is the pacer's and the frame clock's.
class FixedSource : public usbipdcpp::VideoSource
{
  public:
    std::vector<usbipdcpp::VideoFormatInfo> supported_formats() const override
    {
        return {current_format()};
    }
    usbipdcpp::VideoFormatInfo current_format() const override
    {
        return {fourcc,   width,    height,   std::uint32_t(max_frame_size()),
                interval, interval, interval, 16};
    }
    bool set_format(std::uint32_t f, std::uint16_t w, std::uint16_t h, std::uint32_t i) override
    {
        if (f != FOURCC_YUY2 && f != FOURCC_MJPG)
            return false;
        fourcc = f, width = w, height = h, interval = i;
        sets++;
        frame.assign(f == FOURCC_MJPG ? 20000 : std::size_t(w) * h * 2, 0x80);
        return true;
    }
    bool get_frame(usbipdcpp::VideoFrame &f) override
    {
        f = {frame.data(), frame.size(), true};
        return true;
    }
    std::size_t max_frame_size() const override
    {
        return std::size_t(width) * height * 2;
    }
    std::uint32_t frame_interval() const override
    {
        return interval;
    }

    std::uint32_t fourcc = FOURCC_YUY2, interval = 333333;
    std::uint16_t width = 640, height = 480;
    int sets = 0;
    std::vector<std::uint8_t> frame = std::vector<std::uint8_t>(640 * 480 * 2, 0x80);
};

void test_descriptors()
{
    Camera cam(std::make_unique<FixedSource>());
    const Config &c = cam.config();
    CHECK(cam.vendor() == 0x046d && cam.product() == 0x082d);
    CHECK(c.interfaces.size() == 4);
    CHECK(c.vs_interface && *c.vs_interface == 1);
    CHECK(c.processing_unit == 3);
    CHECK(c.formats.size() == 3);
    if (c.formats.size() == 3) {
        CHECK(c.formats[0].fourcc == FOURCC_YUY2 && c.formats[0].frames.size() == 19);
        CHECK(c.formats[1].fourcc == FOURCC_H264 && c.formats[1].frames.size() == 17);
        CHECK(c.formats[2].fourcc == FOURCC_MJPG && c.formats[2].frames.size() == 17);
        const Frame *f = c.formats[0].frame(1);
        CHECK(f && f->width == 640 && f->height == 480 && f->intervals.size() == 7 &&
              f->intervals[0] == 333333);
    }
    // the C920's alternate settings 1 to 11, every microframe, and ours for compressed formats,
    // 12: 3x1024 bytes every 4th microframe (bInterval 3), 13: 3x1022 bytes every 8th (bInterval 4)
    const std::uint32_t ladder[] = {0,    192,  384,  512,  640,  800,  944,
                                    1280, 1600, 1984, 2688, 3060, 3072, 3066};
    for (std::uint8_t alt = 0; alt < 14; alt++)
        CHECK(c.alt_payload(alt) == ladder[alt]);
    for (std::uint8_t alt = 1; alt < 12; alt++)
        CHECK(c.alt_endpoint(alt) && c.alt_endpoint(alt)->interval == 1);
    CHECK(c.alt_endpoint(12) && c.alt_endpoint(12)->interval == 3 &&
          c.alt_endpoint(12)->address == 0x81);
    CHECK(c.alt_endpoint(13) && c.alt_endpoint(13)->interval == 4 &&
          c.alt_endpoint(13)->address == 0x81);
    // uncompressed: never alternate setting 12 or 13, also past the largest payload
    CHECK(c.alt_for(1) == 1 && c.alt_for(2306) == 10 && c.alt_for(3061) == 11 &&
          c.alt_for(5000) == 11);
    // compressed: 13 carries 3064 x 1000 bytes/s, 12 3070 x 2000, no more
    CHECK(c.sparse_alt_for(1) == 13 && c.sparse_alt_for(3064000) == 13 &&
          c.sparse_alt_for(3064001) == 12 && c.sparse_alt_for(6140000) == 12 &&
          c.sparse_alt_for(6140001) == 0);

    Probe r;
    r.format = 1, r.frame = 1, r.interval = 333333;
    Probe p = negotiate(c, r);
    CHECK(p.format == 1 && p.frame == 1 && p.interval == 333333);
    CHECK(p.max_video_frame_size == 640 * 480 * 2);
    CHECK(p.max_payload == 2688); // 614400 B x 30 / 8000 + 2 = 2306: alternate setting 10
    r.interval = 400000;
    CHECK(negotiate(c, r).interval == 416666); // the listed interval nearest
    r.format = 3, r.frame = 1, r.interval = 0;
    p = negotiate(c, r);
    CHECK(p.format == 3 && p.max_payload == 3066 &&
          p.interval == c.formats[2].frame(1)->default_interval);
    // MJPEG 640x480 and 1280x720 at 30 fps: alternate setting 13 (uvcvideo takes the lowest
    // payload of at least 3066, which only 13 has); 1920x1080: 12 (3072, only 12's); YUY2 640x480
    // stays on 10
    for (auto [w, h] : {std::pair{640, 480}, {1280, 720}, {1920, 1080}}) {
        const std::uint32_t want = w == 1920 ? 3072 : 3066;
        const Frame *f = nullptr;
        for (auto &x : c.formats[2].frames)
            if (x.width == w && x.height == h)
                f = &x;
        CHECK(f);
        if (!f)
            continue;
        r.format = 3, r.frame = f->index, r.interval = 333333;
        p = negotiate(c, r);
        CHECK(p.interval == 333333 && p.max_payload == want);
        r.format = 2; // H.264, frame-based: compressed too
        CHECK(negotiate(c, r).max_payload == want);
    }
    // more than 12 carries (2304x1536 MJPEG would be 0.75 bit x 3.5 Mpx x 30): the largest of
    // the C920's own
    Format big;
    big.index = 9, big.kind = Kind::Mjpeg, big.fourcc = FOURCC_MJPG;
    Frame bf;
    bf.index = 1, bf.width = 2304, bf.height = 1536, bf.default_interval = 333333;
    bf.intervals = {333333};
    big.frames.push_back(bf);
    Config c2 = c;
    c2.formats.push_back(big);
    r.format = 9, r.frame = 1, r.interval = 333333;
    CHECK(negotiate(c2, r).max_payload == 3060);
    r.format = 9, r.frame = 77;
    p = negotiate(c, r);
    CHECK(p.format == 1 && p.frame == c.formats[0].default_frame);
    auto bytes = p.bytes();
    CHECK(bytes.size() == 26);
    Probe q = Probe::read(bytes.data(), bytes.size());
    CHECK(q.format == p.format && q.frame == p.frame && q.interval == p.interval &&
          q.max_payload == p.max_payload);
}

void test_pacing()
{
    using std::chrono::microseconds;
    Pacer p(8000, 240);
    CHECK(p.due(microseconds(1000)).send == 8);
    CHECK(p.due(microseconds(1000)).send == 0);
    CHECK(p.due(microseconds(1500)).send == 4);
    auto d = p.due(microseconds(2000));
    CHECK(d.send == 4 && d.skipped == 0);
    CHECK(p.late() == 0);
    d = p.due(microseconds(52000)); // 50 ms late: 400 due, 240 sent, 160 skipped
    CHECK(d.send == 240 && d.skipped == 160);
    CHECK(p.late() == 1 && p.sent() == 256 && p.skipped() == 160);
    CHECK(p.due(microseconds(53000)).send == 8);
    CHECK(iso_rate(true, 1) == 8000 && iso_rate(true, 4) == 1000 && iso_rate(false, 1) == 1000);
}

void test_header()
{
    std::uint8_t h[4] = {2, 0x81, 0, 0}; // usbipdcpp: FID and its "EOF" at bit 7
    fix_payload_header(h, 4);
    CHECK(h[1] == 0x03);
    std::uint8_t g[2] = {2, 0x01};
    fix_payload_header(g, 2);
    CHECK(g[1] == 0x01);
}

void test_jpeg()
{
    const std::uint8_t d[] = {0x00, 0xff, 0xd8, 0xff, 0xe0, 0x12, 0xff,
                              0x00, 0x34, 0xff, 0xd9, 0xff, 0xd8};
    std::size_t begin = 99;
    CHECK(GstSource::jpeg_end(d, sizeof d, begin) == 11 && begin == 1);
    CHECK(GstSource::jpeg_end(d + 11, 2, begin) == 0);
}

std::vector<pid_t> children()
{
    std::vector<pid_t> out;
    if (DIR *dir = opendir("/proc/self/task")) {
        while (auto *e = readdir(dir)) {
            std::ifstream f(std::string("/proc/self/task/") + e->d_name + "/children");
            pid_t pid;
            while (f >> pid)
                out.push_back(pid);
        }
        closedir(dir);
    }
    return out;
}

void test_gst()
{
    if (system("gst-inspect-1.0 videotestsrc >/dev/null 2>&1 && gst-inspect-1.0 jpegenc >/dev/null "
               "2>&1 && "
               "gst-inspect-1.0 timeoverlay >/dev/null 2>&1") != 0) {
        std::fprintf(stderr,
                     "SKIP gst: no gst-launch-1.0 with videotestsrc, jpegenc and timeoverlay\n");
        return;
    }
    auto wait_frame = [](GstSource &s, std::size_t &size) {
        for (int i = 0; i < 100; i++) {
            usbipdcpp::VideoFrame f{};
            if (s.get_frame(f) && s.samples() > 0) {
                size = f.size;
                return true;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
        }
        return false;
    };
    {
        GstSource s("smpte", FOURCC_YUY2, 160, 120, 333333);
        std::size_t size = 0;
        CHECK(wait_frame(s, size) && size == 160 * 120 * 2);
        CHECK(children().size() == 1);
        CHECK(s.set_format(FOURCC_MJPG, 320, 240, 333333)); // the child restarts in the new format
        std::size_t jsize = 0;
        usbipdcpp::VideoFrame f{};
        bool jpeg = false;
        for (int i = 0; i < 100 && !jpeg; i++) {
            if (s.get_frame(f) && f.size > 4 && f.data[0] == 0xff && f.data[1] == 0xd8 &&
                f.data[f.size - 2] == 0xff && f.data[f.size - 1] == 0xd9)
                jpeg = true, jsize = f.size;
            else
                std::this_thread::sleep_for(std::chrono::milliseconds(50));
        }
        CHECK(jpeg && jsize < 320 * 240 * 2);
        CHECK(children().size() == 1); // the old child is gone, not left beside the new one
        CHECK(!s.set_format(FOURCC_H264, 320, 240, 333333));
        std::this_thread::sleep_for(std::chrono::milliseconds(400));
        const auto before = s.samples();
        for (int i = 0; i < 6; i++) {
            s.get_frame(f);
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
        CHECK(s.samples() > before); // live: new frames keep coming
        // asked once a millisecond, as the pacing thread does, it serves GStreamer's 30 frames/s,
        // each once (usbipdcpp's own frame clock would make that 29.4: frame_interval())
        CHECK(s.frame_interval() == 333333 / 2);
        const auto samples0 = s.samples(), repeats0 = s.repeats();
        const auto t0 = std::chrono::steady_clock::now();
        for (int i = 1; i <= 2000; i++) {
            s.get_frame(f);
            std::this_thread::sleep_until(t0 + std::chrono::milliseconds(i));
        }
        const auto served = s.samples() - samples0;
        std::fprintf(stderr, "gst: %llu frames served in 2 s, %llu repeated\n",
                     (unsigned long long)served, (unsigned long long)(s.repeats() - repeats0));
        CHECK(served >= 57 && served <= 63 && s.repeats() == repeats0);
    }
    CHECK(children().empty()); // and ends with the source
}

// ------------------------------------------------------------------ the fake QEMU
struct Guest {
    usbredirparser *p = nullptr;
    int fd = -1;
    bool connected = false, disconnected = false;
    usb_redir_device_connect_header connect{};
    usb_redir_ep_info_header ep{};
    int ep_infos = 0;
    std::map<std::uint64_t, std::pair<int, std::vector<std::uint8_t>>> replies; // id: status, data
    std::uint64_t iso_packets = 0, iso_data = 0, iso_bytes = 0, iso_eof = 0, max_len = 0;
    std::uint64_t next_id = 1;

    static Guest &g(void *priv)
    {
        return *static_cast<Guest *>(priv);
    }

    Guest(int fd_) : fd(fd_)
    {
        p = usbredirparser_create();
        p->priv = this;
        p->read_func = [](void *priv, std::uint8_t *d, int n) -> int {
            const ssize_t r = recv(g(priv).fd, d, std::size_t(n), MSG_DONTWAIT);
            return r > 0 ? int(r) : (r < 0 && errno == EAGAIN) ? 0 : -1;
        };
        p->write_func = [](void *priv, std::uint8_t *d, int n) -> int {
            const ssize_t r = send(g(priv).fd, d, std::size_t(n), MSG_DONTWAIT | MSG_NOSIGNAL);
            return r >= 0 ? int(r) : errno == EAGAIN ? 0 : -1;
        };
        p->device_connect_func = [](void *priv, usb_redir_device_connect_header *h) {
            g(priv).connected = true;
            g(priv).connect = *h;
        };
        p->device_disconnect_func = [](void *priv) { g(priv).disconnected = true; };
        p->hello_func = [](void *, usb_redir_hello_header *) {};
        p->log_func = [](void *, int level, const char *msg) {
            if (level <= usbredirparser_warning)
                std::fprintf(stderr, "fake qemu: %s\n", msg);
        };
        p->interface_info_func = [](void *, usb_redir_interface_info_header *) {};
        p->ep_info_func = [](void *priv, usb_redir_ep_info_header *h) {
            g(priv).ep = *h;
            g(priv).ep_infos++;
        };
        p->configuration_status_func = [](void *priv, std::uint64_t id,
                                          usb_redir_configuration_status_header *h) {
            g(priv).replies[id] = {h->status, {h->configuration}};
        };
        p->alt_setting_status_func = [](void *priv, std::uint64_t id,
                                        usb_redir_alt_setting_status_header *h) {
            g(priv).replies[id] = {h->status, {h->interface, h->alt}};
        };
        p->iso_stream_status_func = [](void *priv, std::uint64_t id,
                                       usb_redir_iso_stream_status_header *h) {
            g(priv).replies[id] = {h->status, {h->endpoint}};
        };
        p->interrupt_receiving_status_func = [](void *priv, std::uint64_t id,
                                                usb_redir_interrupt_receiving_status_header *h) {
            g(priv).replies[id] = {h->status, {h->endpoint}};
        };
        p->control_packet_func = [](void *priv, std::uint64_t id,
                                    usb_redir_control_packet_header *h, std::uint8_t *d, int n) {
            g(priv).replies[id] = {h->status, std::vector<std::uint8_t>(d, d + (d ? n : 0))};
            if (d)
                usbredirparser_free_packet_data(g(priv).p, d);
        };
        p->iso_packet_func = [](void *priv, std::uint64_t, usb_redir_iso_packet_header *h,
                                std::uint8_t *d, int n) {
            auto &s = g(priv);
            s.iso_packets++;
            s.max_len = std::max<std::uint64_t>(s.max_len, h->length);
            if (n > 2) {
                s.iso_data++;
                s.iso_bytes += std::uint64_t(n - d[0]);
            }
            if (n >= 2 && (d[1] & 0x02))
                s.iso_eof++;
            if (d)
                usbredirparser_free_packet_data(s.p, d);
        };
        std::uint32_t caps[USB_REDIR_CAPS_SIZE] = {};
        usbredirparser_caps_set_cap(caps, usb_redir_cap_connect_device_version);
        usbredirparser_caps_set_cap(caps, usb_redir_cap_ep_info_max_packet_size);
        usbredirparser_caps_set_cap(caps, usb_redir_cap_64bits_ids);
        usbredirparser_init(p, "fake qemu", caps, USB_REDIR_CAPS_SIZE, 0);
    }
    ~Guest()
    {
        usbredirparser_destroy(p);
    }

    // pumps both ways until done() or timeout
    bool pump(const std::function<bool()> &done, int timeout_ms = 3000)
    {
        const auto end = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
        while (!done()) {
            if (std::chrono::steady_clock::now() > end)
                return false;
            if (usbredirparser_has_data_to_write(p))
                usbredirparser_do_write(p);
            pollfd f{fd, POLLIN, 0};
            if (poll(&f, 1, 10) > 0 && usbredirparser_do_read(p) == usbredirparser_read_io_error)
                return done();
        }
        return true;
    }
    std::pair<int, std::vector<std::uint8_t>> control(std::uint8_t rt, std::uint8_t req,
                                                      std::uint16_t value, std::uint16_t index,
                                                      std::uint16_t length,
                                                      std::vector<std::uint8_t> out = {})
    {
        usb_redir_control_packet_header h{
            std::uint8_t(rt & 0x80), req, rt, 0, value, index, length};
        const auto id = next_id++;
        usbredirparser_send_control_packet(p, id, &h, out.empty() ? nullptr : out.data(),
                                           int(out.size()));
        pump([&] { return replies.count(id) > 0; });
        return replies.count(id) ? replies[id] : std::make_pair(-1, std::vector<std::uint8_t>{});
    }
    int wait_status(std::uint64_t id)
    {
        pump([&] { return replies.count(id) > 0; });
        return replies.count(id) ? replies[id].first : -1;
    }
};

void test_front()
{
    int sv[2];
    CHECK(socketpair(AF_UNIX, SOCK_STREAM, 0, sv) == 0);
    auto src = std::make_unique<FixedSource>();
    FixedSource *source = src.get();
    Camera cam(std::move(src));
    Front front(sv[0], cam);
    std::thread t([&] { front.run(); });
    Guest guest(sv[1]);

    CHECK(guest.pump([&] { return guest.connected && guest.ep_infos > 0; }));
    CHECK(guest.connect.vendor_id == 0x046d && guest.connect.product_id == 0x082d);
    CHECK(guest.connect.speed == usb_redir_speed_high && guest.connect.device_class == 0xef);
    CHECK(guest.connect.device_version_bcd == 0x0011);
    CHECK(guest.ep.type[16 + 3] == usb_redir_type_interrupt &&
          guest.ep.max_packet_size[16 + 3] == 64);
    CHECK(guest.ep.type[16 + 1] == usb_redir_type_invalid); // VS alt 0: no endpoint

    auto r = guest.control(0x80, 6, 0x0100, 0, 18);
    CHECK(r.first == usb_redir_success && r.second.size() == 18 && r.second[8] == 0x6d);
    r = guest.control(0x80, 6, 0x0200, 0, 9);
    CHECK(r.first == usb_redir_success && r.second.size() == 9 &&
          (r.second[2] | r.second[3] << 8) == 3484); // the C920's 3452, alts 12 and 13
    r = guest.control(0x80, 6, 0x0200, 0, 4096);
    CHECK(r.second.size() == 3484);
    r = guest.control(0x80, 6, 0x0302, 0x0409, 255);
    CHECK(r.first == usb_redir_success && r.second.size() == 38 && r.second[2] == 'H');
    r = guest.control(0x80, 6, 0x0303, 0x0409, 255);
    CHECK(r.first == usb_redir_stall);

    usb_redir_set_configuration_header sc{1};
    std::uint64_t id = guest.next_id++;
    usbredirparser_send_set_configuration(guest.p, id, &sc);
    CHECK(guest.wait_status(id) == usb_redir_success);

    // the processing unit at the C920's unit 3; an extension unit stalls
    r = guest.control(0xa1, 0x86 /* GET_INFO */, 0x0200 /* brightness */, 0x0300, 1);
    CHECK(r.first == usb_redir_success && r.second.size() == 1 && r.second[0] == 0x03);
    r = guest.control(0xa1, 0x81 /* GET_CUR */, 0x0500 /* power line, 1 byte */, 0x0300, 1);
    CHECK(r.first == usb_redir_success && r.second.size() == 1);
    r = guest.control(0xa1, 0x86, 0x0100, 0x0600, 1);
    CHECK(r.first == usb_redir_stall);

    // probe and commit: YUY2 640x480 at 30 fps
    r = guest.control(0xa1, 0x87 /* GET_DEF */, 0x0100, 1, 26);
    CHECK(r.first == usb_redir_success && r.second.size() == 26);
    Probe want;
    want.hint = 1, want.format = 1, want.frame = 1, want.interval = 333333;
    CHECK(guest.control(0x21, 0x01, 0x0100, 1, 26, want.bytes()).first == usb_redir_success);
    r = guest.control(0xa1, 0x81, 0x0100, 1, 26);
    Probe got = Probe::read(r.second.data(), r.second.size());
    CHECK(got.format == 1 && got.frame == 1 && got.max_payload == 2688 &&
          got.max_video_frame_size == 614400);
    CHECK(guest.control(0x21, 0x01, 0x0200, 1, 26, got.bytes()).first == usb_redir_success);
    CHECK(cam.committed().format == 1 && source->width == 640 && source->fourcc == FOURCC_YUY2);

    usb_redir_set_alt_setting_header sa{1, 10};
    id = guest.next_id++;
    const int infos = guest.ep_infos;
    usbredirparser_send_set_alt_setting(guest.p, id, &sa);
    CHECK(guest.wait_status(id) == usb_redir_success);
    CHECK(guest.ep_infos > infos); // before the status
    CHECK(guest.ep.type[16 + 1] == usb_redir_type_iso && guest.ep.max_packet_size[16 + 1] == 2688);
    CHECK(guest.ep.interval[16 + 1] == 1 && guest.ep.interface[16 + 1] == 1);

    usb_redir_start_iso_stream_header si{0x81, 32, 15};
    id = guest.next_id++;
    usbredirparser_send_start_iso_stream(guest.p, id, &si);
    CHECK(guest.wait_status(id) == usb_redir_success);
    const auto t0 = std::chrono::steady_clock::now();
    guest.pump([] { return false; }, 1000);
    const double secs =
        std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
    const double rate = double(guest.iso_packets) / secs, fps = double(guest.iso_eof) / secs;
    std::fprintf(stderr,
                 "stream: %.2f s, %llu packets (%.0f/s), %llu with data, %llu frames (%.1f/s), "
                 "%.1f MB, max %llu\n",
                 secs, (unsigned long long)guest.iso_packets, rate,
                 (unsigned long long)guest.iso_data, (unsigned long long)guest.iso_eof, fps,
                 double(guest.iso_bytes) / 1e6, (unsigned long long)guest.max_len);
    CHECK(rate > 7000 && rate < 9000);
    CHECK(fps > 25 && fps < 35);
    CHECK(guest.max_len <= 2688);
    CHECK(guest.iso_bytes >= guest.iso_eof * 614400 &&
          guest.iso_bytes < (guest.iso_eof + 2) * 614400); // whole frames
    usb_redir_stop_iso_stream_header st{0x81};
    id = guest.next_id++;
    usbredirparser_send_stop_iso_stream(guest.p, id, &st);
    CHECK(guest.wait_status(id) == usb_redir_success);
    CHECK(!front.streaming());

    // format switches: MJPEG 160x90 (frame 2), then the stream again in alternate setting 13 at
    // 1000 packets/s; MJPEG 1920x1080 (frame 17) in 12 at 2000 packets/s (20000-byte frames: 7
    // packets each)
    struct Sparse {
        std::uint8_t frame, alt;
        std::uint32_t payload;
        std::uint8_t interval;
        double rate;
    };
    for (const Sparse x : {Sparse{2, 13, 3066, 4, 1000}, Sparse{17, 12, 3072, 3, 2000}}) {
        want.format = 3, want.frame = x.frame;
        CHECK(guest.control(0x21, 0x01, 0x0200, 1, 26, want.bytes()).first == usb_redir_success);
        CHECK(source->fourcc == FOURCC_MJPG && cam.committed().max_payload == x.payload);
        sa = {1, x.alt};
        id = guest.next_id++;
        usbredirparser_send_set_alt_setting(guest.p, id, &sa);
        CHECK(guest.wait_status(id) == usb_redir_success);
        CHECK(guest.ep.max_packet_size[16 + 1] == x.payload &&
              guest.ep.interval[16 + 1] == x.interval);
        guest.iso_packets = guest.iso_eof = guest.max_len = 0;
        si = {0x81, 20, 8};
        id = guest.next_id++;
        usbredirparser_send_start_iso_stream(guest.p, id, &si);
        CHECK(guest.wait_status(id) == usb_redir_success);
        const auto t1 = std::chrono::steady_clock::now();
        guest.pump([] { return false; }, 1000);
        const double secs2 =
            std::chrono::duration<double>(std::chrono::steady_clock::now() - t1).count();
        const double rate2 = double(guest.iso_packets) / secs2;
        const double fps2 = double(guest.iso_eof) / secs2;
        std::fprintf(stderr, "MJPEG stream, alt %u: %.2f s, %.0f packets/s, %.1f frames/s, max %llu\n",
                     x.alt, secs2, rate2, fps2, (unsigned long long)guest.max_len);
        CHECK(rate2 > 0.875 * x.rate && rate2 < 1.125 * x.rate);
        CHECK(fps2 > 25 && fps2 < 35);
        CHECK(guest.max_len <= x.payload);
        id = guest.next_id++;
        usbredirparser_send_stop_iso_stream(guest.p, id, &st);
        CHECK(guest.wait_status(id) == usb_redir_success);
    }

    front.stop();
    guest.pump([&] { return guest.disconnected; });
    CHECK(guest.disconnected);
    t.join();
    close(sv[0]);
    close(sv[1]);
}

} // namespace

int main()
{
    test_descriptors();
    test_pacing();
    test_header();
    test_jpeg();
    test_gst();
    test_front();
    if (failures)
        std::fprintf(stderr, "%d failure(s)\n", failures);
    else
        std::fprintf(stderr, "all passed\n");
    return failures ? 1 : 0;
}
