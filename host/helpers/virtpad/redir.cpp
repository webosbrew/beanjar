// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
#include "redir.h"

#include <fcntl.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <usbredirparser.h>

#include <cerrno>
#include <chrono>
#include <cstdio>
#include <cstring>

namespace virtpad
{
namespace
{

std::uint8_t redir_status(Status s)
{
    return s == Status::Ok ? usb_redir_success : usb_redir_stall;
}

// ep_info's index of endpoint address ep: 0-15 OUT, 16-31 IN
int ep_index(std::uint8_t ep)
{
    return (ep & 0x0f) | ((ep & 0x80) ? 0x10 : 0);
}

} // namespace

struct Front::Callbacks {
    static Front &self(void *priv)
    {
        return *static_cast<Front *>(priv);
    }

    static void log(void *, int level, const char *msg)
    {
        if (level <= usbredirparser_warning)
            std::fprintf(stderr, "beanjar-virtpad: usbredir: %s\n", msg);
    }
    static int read(void *priv, std::uint8_t *data, int count)
    {
        Front &f = self(priv);
        const ssize_t n = ::recv(f.fd_, data, std::size_t(count), 0);
        if (n > 0)
            return int(n);
        if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR))
            return 0;
        if (n == 0 || errno == ECONNRESET)
            f.peer_closed_ = true;
        else
            f.io_error_ = true;
        return -1;
    }
    static int write(void *priv, std::uint8_t *data, int count)
    {
        Front &f = self(priv);
        const ssize_t n = ::send(f.fd_, data, std::size_t(count), MSG_NOSIGNAL);
        if (n >= 0)
            return int(n);
        if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR)
            return 0;
        if (errno == EPIPE || errno == ECONNRESET)
            f.peer_closed_ = true;
        else
            f.io_error_ = true;
        return -1;
    }

    static void hello(void *priv, usb_redir_hello_header *h)
    {
        Front &f = self(priv);
        std::fprintf(stderr, "beanjar-virtpad: connected to %.64s\n", h->version);
        f.hello_ = true;
        f.announce();
    }
    static void reset(void *priv)
    {
        Front &f = self(priv);
        f.receiving_ = false;
        f.greeted_ = false;
        f.pad_.reset();
    }
    static void set_configuration(void *priv, std::uint64_t id,
                                  usb_redir_set_configuration_header *h)
    {
        Front &f = self(priv);
        usb_redir_configuration_status_header s{};
        s.status = redir_status(f.pad_.set_configuration(h->configuration));
        s.configuration = f.pad_.configuration();
        if (!s.configuration)
            f.receiving_ = false;
        usbredirparser_send_configuration_status(f.parser_, id, &s);
    }
    static void get_configuration(void *priv, std::uint64_t id)
    {
        Front &f = self(priv);
        usb_redir_configuration_status_header s{usb_redir_success, f.pad_.configuration()};
        usbredirparser_send_configuration_status(f.parser_, id, &s);
    }
    static void set_alt_setting(void *priv, std::uint64_t id, usb_redir_set_alt_setting_header *h)
    {
        Front &f = self(priv);
        bool known = false;
        for (auto &i : f.pad_.interfaces())
            known |= i.number == h->interface;
        usb_redir_alt_setting_status_header s{};
        s.status = known && h->alt == 0 ? usb_redir_success : usb_redir_stall;
        s.interface = h->interface;
        s.alt = 0;
        usbredirparser_send_alt_setting_status(f.parser_, id, &s);
    }
    static void get_alt_setting(void *priv, std::uint64_t id, usb_redir_get_alt_setting_header *h)
    {
        Front &f = self(priv);
        bool known = false;
        for (auto &i : f.pad_.interfaces())
            known |= i.number == h->interface;
        usb_redir_alt_setting_status_header s{};
        s.status = known ? usb_redir_success : usb_redir_inval;
        s.interface = h->interface;
        s.alt = known ? 0 : 0xff;
        usbredirparser_send_alt_setting_status(f.parser_, id, &s);
    }
    static void start_iso_stream(void *priv, std::uint64_t id, usb_redir_start_iso_stream_header *h)
    {
        Front &f = self(priv);
        usb_redir_iso_stream_status_header s{usb_redir_inval, h->endpoint};
        usbredirparser_send_iso_stream_status(f.parser_, id, &s);
    }
    static void stop_iso_stream(void *priv, std::uint64_t id, usb_redir_stop_iso_stream_header *h)
    {
        Front &f = self(priv);
        usb_redir_iso_stream_status_header s{usb_redir_inval, h->endpoint};
        usbredirparser_send_iso_stream_status(f.parser_, id, &s);
    }
    static bool interrupt_in(Front &f, std::uint8_t ep)
    {
        for (auto &e : f.pad_.endpoints())
            if (e.address == ep && (e.attributes & 3) == 3 && (ep & 0x80))
                return true;
        return false;
    }
    static void start_interrupt_receiving(void *priv, std::uint64_t id,
                                          usb_redir_start_interrupt_receiving_header *h)
    {
        Front &f = self(priv);
        const bool known = interrupt_in(f, h->endpoint);
        usb_redir_interrupt_receiving_status_header s{
            std::uint8_t(known ? usb_redir_success : usb_redir_inval), h->endpoint};
        usbredirparser_send_interrupt_receiving_status(f.parser_, id, &s);
        if (known && h->endpoint == Pad::INPUT_EP) {
            f.receiving_ = true;
            if (!f.greeted_) {
                for (auto &m : Pad::greeting())
                    f.send_input(m.data(), int(m.size()));
                f.greeted_ = true;
            }
            // what the controls are now, as the controller answers its first poll
            const auto r = Pad::report(f.state_);
            f.send_input(r.data(), int(r.size()));
        }
    }
    static void stop_interrupt_receiving(void *priv, std::uint64_t id,
                                         usb_redir_stop_interrupt_receiving_header *h)
    {
        Front &f = self(priv);
        if (h->endpoint == Pad::INPUT_EP)
            f.receiving_ = false;
        usb_redir_interrupt_receiving_status_header s{usb_redir_success, h->endpoint};
        usbredirparser_send_interrupt_receiving_status(f.parser_, id, &s);
    }
    static void alloc_bulk_streams(void *priv, std::uint64_t id,
                                   usb_redir_alloc_bulk_streams_header *h)
    {
        Front &f = self(priv);
        usb_redir_bulk_streams_status_header s{h->endpoints, 0, usb_redir_inval};
        usbredirparser_send_bulk_streams_status(f.parser_, id, &s);
    }
    static void free_bulk_streams(void *priv, std::uint64_t id,
                                  usb_redir_free_bulk_streams_header *h)
    {
        Front &f = self(priv);
        usb_redir_bulk_streams_status_header s{h->endpoints, 0, usb_redir_inval};
        usbredirparser_send_bulk_streams_status(f.parser_, id, &s);
    }
    static void cancel_data_packet(void *, std::uint64_t)
    {
        // every packet is answered as it comes: nothing is ever outstanding to cancel
    }
    static void control_packet(void *priv, std::uint64_t id, usb_redir_control_packet_header *h,
                               std::uint8_t *data, int)
    {
        Front &f = self(priv);
        const bool in = h->endpoint & 0x80;
        ControlResult r = f.pad_.control(h->requesttype, h->request, h->value, h->index, h->length);
        f.stats_.controls++;
        usb_redir_control_packet_header reply = *h;
        reply.status = redir_status(r.status);
        reply.length = in ? std::uint16_t(r.data.size()) : (r.status == Status::Ok ? h->length : 0);
        usbredirparser_send_control_packet(f.parser_, id, &reply,
                                           in && !r.data.empty() ? r.data.data() : nullptr,
                                           in ? int(r.data.size()) : 0);
        if (data)
            usbredirparser_free_packet_data(f.parser_, data);
    }
    static void bulk_packet(void *priv, std::uint64_t id, usb_redir_bulk_packet_header *h,
                            std::uint8_t *data, int)
    {
        Front &f = self(priv);
        usb_redir_bulk_packet_header reply = *h;
        reply.status = usb_redir_stall;
        reply.length = 0;
        reply.length_high = 0;
        usbredirparser_send_bulk_packet(f.parser_, id, &reply, nullptr, 0);
        if (data)
            usbredirparser_free_packet_data(f.parser_, data);
    }
    static void iso_packet(void *priv, std::uint64_t, usb_redir_iso_packet_header *,
                           std::uint8_t *data, int)
    {
        if (data)
            usbredirparser_free_packet_data(self(priv).parser_, data);
    }
    // an interrupt OUT transfer (QEMU sends IN ones only as start/stop receiving)
    static void interrupt_packet(void *priv, std::uint64_t id, usb_redir_interrupt_packet_header *h,
                                 std::uint8_t *data, int data_len)
    {
        Front &f = self(priv);
        usb_redir_interrupt_packet_header reply = *h;
        if (h->endpoint & 0x80) {
            reply.status = usb_redir_inval;
            reply.length = 0;
        } else {
            if (h->endpoint == Pad::OUTPUT_EP && data && f.pad_.output(data, std::size_t(data_len)))
                f.stats_.outputs++;
            reply.status = usb_redir_success;
        }
        usbredirparser_send_interrupt_packet(f.parser_, id, &reply, nullptr, 0);
        if (data)
            usbredirparser_free_packet_data(f.parser_, data);
    }
};

Front::Front(int fd, Pad &pad) : fd_(fd), pad_(pad)
{
    ::fcntl(fd_, F_SETFL, ::fcntl(fd_, F_GETFL) | O_NONBLOCK);
    parser_ = usbredirparser_create();
    auto *p = parser_;
    p->priv = this;
    p->log_func = Callbacks::log;
    p->read_func = Callbacks::read;
    p->write_func = Callbacks::write;
    p->hello_func = Callbacks::hello;
    p->reset_func = Callbacks::reset;
    p->set_configuration_func = Callbacks::set_configuration;
    p->get_configuration_func = Callbacks::get_configuration;
    p->set_alt_setting_func = Callbacks::set_alt_setting;
    p->get_alt_setting_func = Callbacks::get_alt_setting;
    p->start_iso_stream_func = Callbacks::start_iso_stream;
    p->stop_iso_stream_func = Callbacks::stop_iso_stream;
    p->start_interrupt_receiving_func = Callbacks::start_interrupt_receiving;
    p->stop_interrupt_receiving_func = Callbacks::stop_interrupt_receiving;
    p->alloc_bulk_streams_func = Callbacks::alloc_bulk_streams;
    p->free_bulk_streams_func = Callbacks::free_bulk_streams;
    p->cancel_data_packet_func = Callbacks::cancel_data_packet;
    p->control_packet_func = Callbacks::control_packet;
    p->bulk_packet_func = Callbacks::bulk_packet;
    p->iso_packet_func = Callbacks::iso_packet;
    p->interrupt_packet_func = Callbacks::interrupt_packet;
    std::uint32_t caps[USB_REDIR_CAPS_SIZE] = {};
    usbredirparser_caps_set_cap(caps, usb_redir_cap_connect_device_version);
    usbredirparser_caps_set_cap(caps, usb_redir_cap_ep_info_max_packet_size);
    usbredirparser_caps_set_cap(caps, usb_redir_cap_64bits_ids);
    usbredirparser_caps_set_cap(caps, usb_redir_cap_32bits_bulk_length);
    usbredirparser_init(parser_, "beanjar-virtpad", caps, USB_REDIR_CAPS_SIZE,
                        usbredirparser_fl_usb_host);
}

Front::~Front()
{
    usbredirparser_destroy(parser_);
}

void Front::announce()
{
    usb_redir_interface_info_header ii{};
    for (auto &i : pad_.interfaces()) {
        ii.interface[ii.interface_count] = i.number;
        ii.interface_class[ii.interface_count] = i.cls;
        ii.interface_subclass[ii.interface_count] = i.subclass;
        ii.interface_protocol[ii.interface_count] = i.protocol;
        ii.interface_count++;
    }
    usbredirparser_send_interface_info(parser_, &ii);

    usb_redir_ep_info_header ep{};
    std::memset(ep.type, usb_redir_type_invalid, sizeof ep.type);
    ep.type[0] = ep.type[16] = usb_redir_type_control;
    ep.max_packet_size[0] = ep.max_packet_size[16] = pad_.device_descriptor()[7];
    for (auto &e : pad_.endpoints()) {
        const int i = ep_index(e.address);
        ep.type[i] = e.attributes & 3;
        ep.interval[i] = e.interval;
        ep.interface[i] = e.interface;
        ep.max_packet_size[i] = e.max_packet;
    }
    usbredirparser_send_ep_info(parser_, &ep);

    const auto &d = pad_.device_descriptor();
    usb_redir_device_connect_header c{};
    c.speed = usb_redir_speed_full;
    c.device_class = d[4];
    c.device_subclass = d[5];
    c.device_protocol = d[6];
    c.vendor_id = Pad::VENDOR;
    c.product_id = Pad::PRODUCT;
    c.device_version_bcd = pad_.bcd_device();
    usbredirparser_send_device_connect(parser_, &c);
}

void Front::send_input(const std::uint8_t *data, int length)
{
    usb_redir_interrupt_packet_header h{Pad::INPUT_EP, usb_redir_success, std::uint16_t(length)};
    usbredirparser_send_interrupt_packet(parser_, next_id_++, &h, const_cast<std::uint8_t *>(data),
                                         length);
}

void Front::set_state(const State &state)
{
    if (state == state_)
        return;
    state_ = state;
    if (!receiving_)
        return;
    const auto r = Pad::report(state_);
    send_input(r.data(), int(r.size()));
    stats_.reports++;
}

bool Front::want_write() const
{
    return usbredirparser_has_data_to_write(parser_);
}

bool Front::io()
{
    if (!peer_closed_ && !io_error_) {
        const int r = usbredirparser_do_read(parser_);
        if (r == usbredirparser_read_parse_error)
            std::fprintf(stderr, "beanjar-virtpad: usbredir: a packet did not parse; skipped\n");
        else if (r == usbredirparser_read_io_error && !peer_closed_)
            io_error_ = true;
    }
    if (!peer_closed_ && !io_error_ && usbredirparser_has_data_to_write(parser_))
        usbredirparser_do_write(parser_);
    return !peer_closed_ && !io_error_;
}

void Front::disconnect(int timeout_ms)
{
    if (peer_closed_ || io_error_)
        return;
    usbredirparser_send_device_disconnect(parser_);
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
    while (usbredirparser_has_data_to_write(parser_) && !peer_closed_ && !io_error_) {
        if (usbredirparser_do_write(parser_) < 0)
            return;
        const auto left = std::chrono::duration_cast<std::chrono::milliseconds>(
                              deadline - std::chrono::steady_clock::now())
                              .count();
        if (left <= 0)
            return;
        pollfd p{fd_, POLLOUT, 0};
        ::poll(&p, 1, int(left));
    }
}

} // namespace virtpad
