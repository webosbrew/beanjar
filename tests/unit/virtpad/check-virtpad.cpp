// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
// The virtual gamepad's parts without a VM (host/helpers/virtpad): the controller's descriptors
// read back, the standard and vendor requests, the input report's layout as xpad reads it, the
// control names, and the usbredir front end against a fake QEMU (a libusbredirparser in the guest
// role over a socket pair): enumeration at full speed, configuration, the interrupt endpoint's
// greeting and reports, LED and rumble on the output endpoint, and the unplug.
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <usbredirparser.h>

#include <chrono>
#include <cstdio>
#include <cstring>
#include <functional>
#include <map>
#include <string>
#include <vector>

#include "pad.h"
#include "redir.h"

using namespace virtpad;

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

void test_descriptors()
{
    Pad pad;
    const auto &d = pad.device_descriptor();
    CHECK(d.size() == 18 && d[0] == 18 && d[1] == 1);
    CHECK((d[8] | d[9] << 8) == 0x045e && (d[10] | d[11] << 8) == 0x028e);
    CHECK(d[4] == 0xff && d[5] == 0xff && d[6] == 0xff && d[7] == 8);
    CHECK(pad.bcd_device() == 0x0114);
    const auto &c = pad.config_descriptor();
    CHECK(c.size() == 153 && (c[2] | c[3] << 8) == 153 && c[4] == 4 && c[5] == 1);
    // the descriptors chain to the end exactly
    std::size_t at = 0;
    while (at < c.size() && c[at] >= 2)
        at += c[at];
    CHECK(at == c.size());
    CHECK(pad.interfaces().size() == 4);
    CHECK(pad.interfaces()[0].cls == 0xff && pad.interfaces()[0].subclass == 0x5d &&
          pad.interfaces()[0].protocol == 0x01);
    CHECK(pad.interfaces()[3].subclass == 0xfd && pad.interfaces()[3].protocol == 0x13);
    CHECK(pad.endpoints().size() == 7);
    const auto &in = pad.endpoints()[0];
    CHECK(in.interface == 0 && in.address == 0x81 && in.attributes == 3 && in.interval == 4 &&
          in.max_packet == 32);
    const auto &out = pad.endpoints()[1];
    CHECK(out.interface == 0 && out.address == 0x01 && out.interval == 8);
    CHECK(pad.endpoints()[6].interface == 2 && pad.endpoints()[6].address == 0x84);
}

void test_requests()
{
    Pad pad;
    auto r = pad.control(0x80, 6, 0x0100, 0, 64);
    CHECK(r.status == Status::Ok && r.data.size() == 18);
    r = pad.control(0x80, 6, 0x0100, 0, 8); // the first read, bMaxPacketSize0
    CHECK(r.data.size() == 8 && r.data[7] == 8);
    r = pad.control(0x80, 6, 0x0200, 0, 9);
    CHECK(r.status == Status::Ok && r.data.size() == 9);
    r = pad.control(0x80, 6, 0x0200, 0, 0xffff);
    CHECK(r.data.size() == 153);
    r = pad.control(0x80, 6, 0x0300, 0, 255);
    CHECK(r.status == Status::Ok && r.data == (std::vector<std::uint8_t>{4, 3, 0x09, 0x04}));
    r = pad.control(0x80, 6, 0x0302, 0x0409, 255);
    CHECK(r.status == Status::Ok && r.data.size() == 22 && r.data[1] == 3 && r.data[2] == 'C');
    r = pad.control(0x80, 6, 0x0301, 0x0409, 255);
    CHECK(r.data.size() == 2 + 2 * 22 && r.data[2] == 0xa9 && r.data[3] == 0); // ©
    r = pad.control(0x80, 6, 0x0304, 0x0409, 255);
    CHECK(r.status == Status::Ok && r.data[0] == r.data.size() && r.data[2] == 'X');
    r = pad.control(0x80, 6, 0x0305, 0x0409, 255);
    CHECK(r.status == Status::Stall);
    r = pad.control(0x80, 6, 0x0600, 0, 10); // no device qualifier at full speed
    CHECK(r.status == Status::Stall);
    r = pad.control(0x80, 0, 0, 0, 2);
    CHECK(r.status == Status::Ok && r.data == (std::vector<std::uint8_t>{0, 0}));
    CHECK(pad.control(0x00, 3, 1, 0, 0).status == Status::Ok); // remote wakeup on
    r = pad.control(0x80, 0, 0, 0, 2);
    CHECK(r.data == (std::vector<std::uint8_t>{2, 0}));
    // xpad's "magic message" at open: the input capabilities, 20 bytes
    r = pad.control(0xc1, 1, 0x0100, 0, 20);
    CHECK(r.status == Status::Ok && r.data.size() == 20 && r.data[0] == 0 && r.data[1] == 0x14);
    r = pad.control(0xc1, 1, 0x0000, 0, 8);
    CHECK(r.status == Status::Ok && r.data.size() == 8);
    CHECK(pad.control(0xc1, 2, 0, 0, 8).status == Status::Stall);
    CHECK(pad.set_configuration(2) == Status::Stall);
    CHECK(pad.set_configuration(1) == Status::Ok && pad.configuration() == 1);
}

void test_report()
{
    State s;
    s.press(*control_named("a"));
    s.press(*control_named("up"));
    s.press(*control_named("rt"));
    s.lx = 20000;
    s.ly = -1;
    const auto r = Pad::report(s);
    CHECK(r[0] == 0x00 && r[1] == 0x14);
    CHECK(r[2] == 0x01 && r[3] == 0x10); // up in byte 2 bit 0, A in byte 3 bit 4
    CHECK(r[4] == 0 && r[5] == 0xff);
    CHECK((r[6] | r[7] << 8) == 20000 && r[8] == 0xff && r[9] == 0xff);
    for (int i = 14; i < 20; i++)
        CHECK(r[i] == 0);
    s.release(*control_named("a"));
    s.release(*control_named("rt"));
    CHECK(Pad::report(s)[3] == 0 && Pad::report(s)[5] == 0);
    for (const char *n : {"b", "x", "y", "lb", "rb", "lt", "back", "start", "guide", "ls", "rs",
                          "down", "left", "right"})
        CHECK(control_named(n).has_value());
    CHECK(!control_named("z") && !control_named("A") && !control_named(""));
    CHECK(control_named("guide")->button == 0x0400 && control_named("y")->button == 0x8000);
    CHECK(control_named("lt")->trigger == 1 && control_named("lt")->button == 0);

    Pad pad;
    CHECK(pad.led() == 0x0e);
    const std::uint8_t led[] = {0x01, 0x03, 0x06};
    CHECK(pad.output(led, sizeof led) && pad.led() == 0x06);
    const std::uint8_t rumble[] = {0x00, 0x08, 0x00, 0x40, 0x80, 0x00, 0x00, 0x00};
    CHECK(pad.output(rumble, sizeof rumble) && pad.rumble_big() == 0x40 &&
          pad.rumble_small() == 0x80);
    const std::uint8_t other[] = {0x02, 0x08, 0x03};
    CHECK(!pad.output(other, sizeof other));
}

// ------------------------------------------------------------------ the fake QEMU
struct Guest {
    usbredirparser *p = nullptr;
    int fd = -1;
    bool connected = false, disconnected = false;
    usb_redir_device_connect_header connect{};
    usb_redir_ep_info_header ep{};
    usb_redir_interface_info_header intf{};
    std::map<std::uint64_t, std::pair<int, std::vector<std::uint8_t>>> replies; // id: status, data
    std::vector<std::vector<std::uint8_t>> interrupts;                          // on 0x81
    std::uint64_t next_id = 1;

    static Guest &g(void *priv)
    {
        return *static_cast<Guest *>(priv);
    }

    explicit Guest(int fd_) : fd(fd_)
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
        p->interface_info_func = [](void *priv, usb_redir_interface_info_header *h) {
            g(priv).intf = *h;
        };
        p->ep_info_func = [](void *priv, usb_redir_ep_info_header *h) { g(priv).ep = *h; };
        p->configuration_status_func = [](void *priv, std::uint64_t id,
                                          usb_redir_configuration_status_header *h) {
            g(priv).replies[id] = {h->status, {h->configuration}};
        };
        p->alt_setting_status_func = [](void *priv, std::uint64_t id,
                                        usb_redir_alt_setting_status_header *h) {
            g(priv).replies[id] = {h->status, {h->interface, h->alt}};
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
        p->interrupt_packet_func = [](void *priv, std::uint64_t id,
                                      usb_redir_interrupt_packet_header *h, std::uint8_t *d,
                                      int n) {
            auto &s = g(priv);
            if (h->endpoint == 0x81)
                s.interrupts.emplace_back(d, d + (d ? n : 0));
            else
                s.replies[id] = {h->status, {h->endpoint}};
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

    // pumps both sides until done() or timeout
    bool pump(Front &front, const std::function<bool()> &done, int timeout_ms = 3000)
    {
        const auto end = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
        while (!done()) {
            if (std::chrono::steady_clock::now() > end)
                return false;
            if (usbredirparser_has_data_to_write(p))
                usbredirparser_do_write(p);
            front.io();
            pollfd f{fd, POLLIN, 0};
            if (poll(&f, 1, 5) > 0 && usbredirparser_do_read(p) == usbredirparser_read_io_error)
                return done();
        }
        return true;
    }
    std::pair<int, std::vector<std::uint8_t>> control(Front &front, std::uint8_t rt,
                                                      std::uint8_t req, std::uint16_t value,
                                                      std::uint16_t index, std::uint16_t length)
    {
        usb_redir_control_packet_header h{
            std::uint8_t(rt & 0x80), req, rt, 0, value, index, length};
        const auto id = next_id++;
        usbredirparser_send_control_packet(p, id, &h, nullptr, 0);
        pump(front, [&] { return replies.count(id) > 0; });
        return replies.count(id) ? replies[id] : std::make_pair(-1, std::vector<std::uint8_t>{});
    }
    int wait_status(Front &front, std::uint64_t id)
    {
        pump(front, [&] { return replies.count(id) > 0; });
        return replies.count(id) ? replies[id].first : -1;
    }
};

void test_front()
{
    int sv[2];
    CHECK(socketpair(AF_UNIX, SOCK_STREAM, 0, sv) == 0);
    Pad pad;
    Front front(sv[0], pad);
    Guest guest(sv[1]);

    CHECK(guest.pump(front, [&] { return guest.connected; }));
    CHECK(front.connected() && !front.configured() && !front.polled());
    CHECK(guest.connect.vendor_id == 0x045e && guest.connect.product_id == 0x028e);
    CHECK(guest.connect.speed == usb_redir_speed_full && guest.connect.device_class == 0xff);
    CHECK(guest.connect.device_version_bcd == 0x0114);
    CHECK(guest.intf.interface_count == 4 && guest.intf.interface_subclass[0] == 0x5d &&
          guest.intf.interface_protocol[0] == 1);
    CHECK(guest.ep.type[16 + 1] == usb_redir_type_interrupt && guest.ep.interval[16 + 1] == 4 &&
          guest.ep.max_packet_size[16 + 1] == 32);
    CHECK(guest.ep.type[1] == usb_redir_type_interrupt && guest.ep.interval[1] == 8);
    CHECK(guest.ep.type[16 + 4] == usb_redir_type_interrupt && guest.ep.interface[16 + 4] == 2);
    CHECK(guest.ep.type[16 + 5] == usb_redir_type_invalid && guest.ep.max_packet_size[0] == 8);

    auto r = guest.control(front, 0x80, 6, 0x0100, 0, 18);
    CHECK(r.first == usb_redir_success && r.second.size() == 18 && r.second[10] == 0x8e);
    r = guest.control(front, 0x80, 6, 0x0200, 0, 255);
    CHECK(r.first == usb_redir_success && r.second.size() == 153);
    r = guest.control(front, 0x80, 6, 0x0600, 0, 10);
    CHECK(r.first == usb_redir_stall);

    // a press before the guest polls: nothing is sent
    State s;
    s.press(*control_named("b"));
    front.set_state(s);
    CHECK(front.stats().reports == 0);

    usb_redir_set_configuration_header sc{1};
    std::uint64_t id = guest.next_id++;
    usbredirparser_send_set_configuration(guest.p, id, &sc);
    CHECK(guest.wait_status(front, id) == usb_redir_success && front.configured());
    r = guest.control(front, 0xc1, 1, 0x0100, 0, 20);
    CHECK(r.first == usb_redir_success && r.second.size() == 20);

    // xpad opens: the interrupt IN endpoint is polled; the greeting, then the state (b held)
    usb_redir_start_interrupt_receiving_header start{0x81};
    id = guest.next_id++;
    usbredirparser_send_start_interrupt_receiving(guest.p, id, &start);
    CHECK(guest.wait_status(front, id) == usb_redir_success && front.polled());
    CHECK(guest.pump(front, [&] { return guest.interrupts.size() >= 5; }));
    CHECK(guest.interrupts.size() == 5);
    if (guest.interrupts.size() == 5) {
        CHECK(guest.interrupts[0] == (std::vector<std::uint8_t>{0x01, 0x03, 0x0e}));
        CHECK(guest.interrupts[3] == (std::vector<std::uint8_t>{0x08, 0x03, 0x00}));
        CHECK(guest.interrupts[4].size() == 20 && guest.interrupts[4][3] == 0x20);
    }
    // the other IN endpoints start and stay silent; an unknown one is refused
    usb_redir_start_interrupt_receiving_header start4{0x84};
    id = guest.next_id++;
    usbredirparser_send_start_interrupt_receiving(guest.p, id, &start4);
    CHECK(guest.wait_status(front, id) == usb_redir_success);
    usb_redir_start_interrupt_receiving_header start9{0x89};
    id = guest.next_id++;
    usbredirparser_send_start_interrupt_receiving(guest.p, id, &start9);
    CHECK(guest.wait_status(front, id) == usb_redir_inval);

    // 10 presses and releases: 20 reports, in order; an unchanged state sends nothing
    guest.interrupts.clear();
    for (int i = 0; i < 10; i++) {
        State on;
        on.press(*control_named("a"));
        front.set_state(on);
        front.set_state(on);
        front.set_state(State{});
    }
    CHECK(guest.pump(front, [&] { return guest.interrupts.size() >= 20; }));
    guest.pump(front, [] { return false; }, 100); // and nothing more
    CHECK(guest.interrupts.size() == 20); // (a, b released), none, then 9 x (a, none)
    CHECK(front.stats().reports == 20);
    int downs = 0, ups = 0;
    for (const auto &rep : guest.interrupts) {
        CHECK(rep.size() == 20 && rep[0] == 0 && rep[1] == 0x14 && !(rep[3] & 0x20));
        (rep[3] & 0x10 ? downs : ups)++;
    }
    CHECK(downs == 10 && ups == 10);

    // the LED message on the output endpoint
    usb_redir_interrupt_packet_header out{0x01, 0, 3};
    std::uint8_t led[] = {0x01, 0x03, 0x02};
    id = guest.next_id++;
    usbredirparser_send_interrupt_packet(guest.p, id, &out, led, 3);
    CHECK(guest.wait_status(front, id) == usb_redir_success);
    CHECK(pad.led() == 0x02 && front.stats().outputs == 1);

    // unplugged: a device_disconnect reaches QEMU
    front.disconnect(1000);
    CHECK(guest.pump(front, [&] { return guest.disconnected; }));
    close(sv[1]);
    CHECK(guest.pump(front, [&] { return front.closed(); }, 1000) || front.closed());
    close(sv[0]);
}

} // namespace

int main()
{
    test_descriptors();
    test_requests();
    test_report();
    test_front();
    if (failures)
        std::fprintf(stderr, "check-virtpad: %d failed\n", failures);
    else
        std::printf("check-virtpad: all passed\n");
    return failures ? 1 : 0;
}
