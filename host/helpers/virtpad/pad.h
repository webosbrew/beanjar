// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
// The pad as a USB device: a wired Xbox 360 controller (045e:028e), with that controller's own
// descriptors byte for byte (descriptors.h), so the guest's own xpad driver binds it and LG's PDM
// sees an "XPAD" device. No USB library: the device has one configuration, no alternate settings
// and only interrupt endpoints, so the standard requests are answered here from the descriptors.
//
// Its input report is the controller's 20 bytes on endpoint 0x81 (byte 0 type 0x00, byte 1 length
// 0x14, bytes 2-3 the buttons, 4-5 the triggers, 6-13 the sticks); its output on endpoint 0x01 is
// the LED (01 03 xx) and rumble (00 08 00 bb ll 00 00 00) messages, kept for show.
#pragma once

#include <array>
#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace virtpad
{

enum class Status { Ok, Stall };

struct ControlResult {
    Status status = Status::Stall;
    std::vector<std::uint8_t> data; // IN: at most the request's length
};

// A control of the pad by its name (a, b, x, y, lb, rb, lt, rt, back, start, guide, ls, rs, up,
// down, left, right): a bit of the buttons, or a trigger pulled all the way.
struct Control {
    std::uint16_t button = 0; // the report's bytes 2-3, little-endian
    std::uint8_t trigger = 0; // 1 left, 2 right
};
std::optional<Control> control_named(std::string_view name);

struct State {
    std::uint16_t buttons = 0;
    std::uint8_t left_trigger = 0, right_trigger = 0;
    std::int16_t lx = 0, ly = 0, rx = 0, ry = 0;

    void press(const Control &c);
    void release(const Control &c);
    bool operator==(const State &) const = default;
};

struct Endpoint {
    std::uint8_t interface, address, attributes, interval;
    std::uint16_t max_packet;
};

class Pad
{
  public:
    Pad();

    static constexpr std::uint16_t VENDOR = 0x045e, PRODUCT = 0x028e;
    static constexpr std::uint8_t INPUT_EP = 0x81, OUTPUT_EP = 0x01;
    static constexpr std::size_t REPORT_SIZE = 20;

    const std::vector<std::uint8_t> &device_descriptor() const;
    const std::vector<std::uint8_t> &config_descriptor() const;
    std::uint16_t bcd_device() const;
    // every endpoint of the configuration, by interface (none has alternate settings)
    const std::vector<Endpoint> &endpoints() const
    {
        return endpoints_;
    }
    // (number, class, subclass, protocol) of each interface
    struct Interface {
        std::uint8_t number, cls, subclass, protocol;
    };
    const std::vector<Interface> &interfaces() const
    {
        return interfaces_;
    }

    // the standard requests usbredir carries in packets of their own
    Status set_configuration(std::uint8_t value);
    std::uint8_t configuration() const
    {
        return configuration_;
    }
    // every other control request, as on endpoint 0
    ControlResult control(std::uint8_t requesttype, std::uint8_t request, std::uint16_t value,
                          std::uint16_t index, std::uint16_t length);
    // a message from the host on the output endpoint (LED, rumble): whether it was one
    bool output(const std::uint8_t *data, std::size_t length);
    void reset();

    // the input report of state
    static std::array<std::uint8_t, REPORT_SIZE> report(const State &state);
    // the messages the controller sends once it is configured, before its first input report:
    // the LED pattern (01 03 0e), then 02 03 00, 03 03 03 and 08 03 00 (no headset)
    static std::vector<std::vector<std::uint8_t>> greeting();

    std::uint8_t led() const
    {
        return led_;
    }
    std::uint8_t rumble_big() const
    {
        return rumble_[0];
    }
    std::uint8_t rumble_small() const
    {
        return rumble_[1];
    }

  private:
    std::vector<Endpoint> endpoints_;
    std::vector<Interface> interfaces_;
    std::uint8_t configuration_ = 0;
    bool remote_wakeup_ = false;
    std::uint8_t led_ = 0x0e; // what it reports at power-up
    std::uint8_t rumble_[2] = {0, 0};
};

// UTF-16LE string descriptor of text (UTF-8)
std::vector<std::uint8_t> string_descriptor(std::string_view text);

} // namespace virtpad
