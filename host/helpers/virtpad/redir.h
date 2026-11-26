// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
// The pad's usbredir end: the usb-host role of the usbredir protocol (libusbredirparser with
// usbredirparser_fl_usb_host), talking to a QEMU usb-redir device's chardev, with the Pad behind
// it, as host/helpers/virtcam/redir.cpp does for the camera.
//
// After QEMU's hello it announces the device (interface_info, ep_info, device_connect, at full
// speed, as the controller is). Configuration requests come as their own packets, the rest as
// control packets, each answered at once from the Pad. Interrupt IN endpoints are started when
// the guest's driver polls them (xpad does while its input device is open); on 0x81 the
// controller's greeting goes first, then each change of the state as one input report, as the
// controller sends only on change. The other IN endpoints never send. Interrupt OUT packets (LED,
// rumble) are taken by the Pad and acknowledged.
//
// Single-threaded: the owner polls fd() (and want_write()) and calls io().
#pragma once

#include <cstdint>

#include "pad.h"

struct usbredirparser;

namespace virtpad
{

struct Stats {
    std::uint64_t reports = 0;  // input reports sent on 0x81
    std::uint64_t outputs = 0;  // messages taken on the output endpoint (LED, rumble)
    std::uint64_t controls = 0; // control requests answered
};

class Front
{
  public:
    // fd: a connected stream socket to QEMU's chardev (made non-blocking here)
    Front(int fd, Pad &pad);
    ~Front();
    Front(const Front &) = delete;
    Front &operator=(const Front &) = delete;

    int fd() const
    {
        return fd_;
    }
    bool want_write() const;
    // reads and writes what is ready; false once the peer closed or an I/O error
    bool io();
    // the pad's controls changed: an input report if the guest polls the pad
    void set_state(const State &state);
    // a device_disconnect, flushed for up to timeout_ms
    void disconnect(int timeout_ms);

    bool connected() const
    {
        return hello_;
    }
    bool configured() const
    {
        return pad_.configuration() != 0;
    }
    bool polled() const
    {
        return receiving_;
    }
    bool closed() const
    {
        return peer_closed_;
    }
    bool failed() const
    {
        return io_error_;
    }
    const Stats &stats() const
    {
        return stats_;
    }

    struct Callbacks;

  private:
    friend struct Callbacks;
    void announce();
    void send_input(const std::uint8_t *data, int length);

    int fd_;
    Pad &pad_;
    usbredirparser *parser_;
    State state_;
    bool hello_ = false, receiving_ = false, greeted_ = false;
    bool peer_closed_ = false, io_error_ = false;
    std::uint64_t next_id_ = 0;
    Stats stats_;
};

} // namespace virtpad
