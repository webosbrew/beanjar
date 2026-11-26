// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
#include "pad.h"

#include <algorithm>

#include "descriptors.h"

namespace virtpad
{
namespace
{

struct Named {
    std::string_view name;
    Control control;
};

// xpad360_process_packet's bits (the guest kernel's drivers/input/joystick/xpad.c)
constexpr Named NAMED[] = {
    {"up", {0x0001, 0}},    {"down", {0x0002, 0}},   {"left", {0x0004, 0}},
    {"right", {0x0008, 0}}, {"start", {0x0010, 0}},  {"back", {0x0020, 0}},
    {"ls", {0x0040, 0}},    {"rs", {0x0080, 0}},     {"lb", {0x0100, 0}},
    {"rb", {0x0200, 0}},    {"guide", {0x0400, 0}},  {"a", {0x1000, 0}},
    {"b", {0x2000, 0}},     {"x", {0x4000, 0}},      {"y", {0x8000, 0}},
    {"lt", {0, 1}},         {"rt", {0, 2}},
};

constexpr std::uint8_t STD_IN_DEVICE = 0x80, STD_IN_INTERFACE = 0x81, STD_IN_ENDPOINT = 0x82;
constexpr std::uint8_t STD_OUT_DEVICE = 0x00, STD_OUT_INTERFACE = 0x01, STD_OUT_ENDPOINT = 0x02;
constexpr std::uint8_t VENDOR_IN_INTERFACE = 0xc1;
constexpr std::uint8_t GET_STATUS = 0, CLEAR_FEATURE = 1, SET_FEATURE = 3, GET_DESCRIPTOR = 6;
constexpr std::uint16_t DEVICE_REMOTE_WAKEUP = 1, ENDPOINT_HALT = 0;

ControlResult ok(std::vector<std::uint8_t> data, std::uint16_t length)
{
    if (data.size() > length)
        data.resize(length);
    return {Status::Ok, std::move(data)};
}

void put16(std::uint8_t *p, std::uint16_t v)
{
    p[0] = std::uint8_t(v);
    p[1] = std::uint8_t(v >> 8);
}

} // namespace

std::optional<Control> control_named(std::string_view name)
{
    for (const auto &n : NAMED)
        if (n.name == name)
            return n.control;
    return std::nullopt;
}

void State::press(const Control &c)
{
    buttons |= c.button;
    if (c.trigger == 1)
        left_trigger = 0xff;
    if (c.trigger == 2)
        right_trigger = 0xff;
}

void State::release(const Control &c)
{
    buttons &= std::uint16_t(~c.button);
    if (c.trigger == 1)
        left_trigger = 0;
    if (c.trigger == 2)
        right_trigger = 0;
}

std::vector<std::uint8_t> string_descriptor(std::string_view text)
{
    std::vector<std::uint8_t> out{0, 3};
    for (std::size_t i = 0; i < text.size();) {
        const auto c = static_cast<unsigned char>(text[i]);
        std::uint32_t cp = c;
        std::size_t n = 1;
        if (c >= 0xe0 && i + 2 < text.size())
            cp = (c & 0x0f) << 12 | (text[i + 1] & 0x3f) << 6 | (text[i + 2] & 0x3f), n = 3;
        else if (c >= 0xc0 && i + 1 < text.size())
            cp = (c & 0x1f) << 6 | (text[i + 1] & 0x3f), n = 2;
        i += n;
        out.push_back(std::uint8_t(cp));
        out.push_back(std::uint8_t(cp >> 8));
    }
    out[0] = std::uint8_t(std::min<std::size_t>(out.size(), 0xfe));
    out.resize(out[0]);
    return out;
}

Pad::Pad()
{
    const auto &c = config_descriptor();
    std::uint8_t interface = 0;
    for (std::size_t i = 0; i + 1 < c.size() && c[i] >= 2; i += c[i]) {
        if (c[i + 1] == 4 && i + 8 < c.size()) {
            interface = c[i + 2];
            interfaces_.push_back({c[i + 2], c[i + 5], c[i + 6], c[i + 7]});
        } else if (c[i + 1] == 5 && i + 6 < c.size()) {
            endpoints_.push_back({interface, c[i + 2], c[i + 3], c[i + 6],
                                  std::uint16_t(c[i + 4] | c[i + 5] << 8)});
        }
    }
}

const std::vector<std::uint8_t> &Pad::device_descriptor() const
{
    static const std::vector<std::uint8_t> d(std::begin(descriptors::device),
                                             std::end(descriptors::device));
    return d;
}

const std::vector<std::uint8_t> &Pad::config_descriptor() const
{
    static const std::vector<std::uint8_t> d(std::begin(descriptors::configuration),
                                             std::end(descriptors::configuration));
    return d;
}

std::uint16_t Pad::bcd_device() const
{
    return std::uint16_t(descriptors::device[12] | descriptors::device[13] << 8);
}

Status Pad::set_configuration(std::uint8_t value)
{
    if (value != 0 && value != descriptors::configuration[5])
        return Status::Stall;
    configuration_ = value;
    return Status::Ok;
}

void Pad::reset()
{
    configuration_ = 0;
    remote_wakeup_ = false;
}

ControlResult Pad::control(std::uint8_t requesttype, std::uint8_t request, std::uint16_t value,
                           std::uint16_t index, std::uint16_t length)
{
    const std::uint8_t type = value >> 8, number = value & 0xff;
    if (requesttype == STD_IN_DEVICE && request == GET_DESCRIPTOR) {
        if (type == 1)
            return ok(device_descriptor(), length);
        if (type == 2 && number == 0)
            return ok(config_descriptor(), length);
        if (type == 3 && number == 0)
            return ok({4, 3, std::uint8_t(descriptors::LANGID), descriptors::LANGID >> 8}, length);
        if (type == 3 && number <= std::size(descriptors::strings))
            return ok(string_descriptor(descriptors::strings[number - 1]), length);
        // a full-speed device has no device qualifier or other-speed configuration
        return {};
    }
    if (requesttype == STD_IN_DEVICE && request == GET_STATUS)
        return ok({std::uint8_t(remote_wakeup_ ? 2 : 0), 0}, length); // bus-powered
    if ((requesttype == STD_IN_INTERFACE || requesttype == STD_IN_ENDPOINT) &&
        request == GET_STATUS)
        return ok({0, 0}, length);
    if (requesttype == STD_OUT_DEVICE && (request == SET_FEATURE || request == CLEAR_FEATURE) &&
        value == DEVICE_REMOTE_WAKEUP) {
        remote_wakeup_ = request == SET_FEATURE;
        return {Status::Ok, {}};
    }
    if (requesttype == STD_OUT_ENDPOINT && (request == SET_FEATURE || request == CLEAR_FEATURE) &&
        value == ENDPOINT_HALT)
        return {Status::Ok, {}};
    if (requesttype == STD_OUT_INTERFACE && (request == SET_FEATURE || request == CLEAR_FEATURE))
        return {};
    // XInput's capabilities (bRequest 1 to interface 0), which xpad reads once at open as its
    // "magic message" and SDL parses: wValue 0x0100 the input's, a report of the controls the
    // pad has (every button but bit 11, the triggers' and sticks' resolution), and wValue 0 the
    // output's (both rumble motors). The layout is SDL's; no capture of a genuine pad's answer
    // was found, so the values are the controls this pad has, at full resolution.
    if (requesttype == VENDOR_IN_INTERFACE && request == 1 && index == 0) {
        if (value == 0x0100) {
            std::vector<std::uint8_t> caps(20, 0);
            caps[0] = 0x00;
            caps[1] = 0x14;
            put16(&caps[2], 0xf7ff);
            caps[4] = caps[5] = 0xff;
            for (int i = 0; i < 4; i++)
                put16(&caps[6 + 2 * i], 0xffc0);
            return ok(caps, length);
        }
        if (value == 0x0000)
            return ok({0x00, 0x08, 0x00, 0xff, 0xff, 0x00, 0x00, 0x00}, length);
    }
    return {};
}

bool Pad::output(const std::uint8_t *data, std::size_t length)
{
    if (length >= 3 && data[0] == 0x01 && data[1] == 0x03) {
        led_ = data[2];
        return true;
    }
    if (length >= 5 && data[0] == 0x00 && data[1] == 0x08) {
        rumble_[0] = data[3];
        rumble_[1] = data[4];
        return true;
    }
    return false;
}

std::array<std::uint8_t, Pad::REPORT_SIZE> Pad::report(const State &s)
{
    std::array<std::uint8_t, REPORT_SIZE> r{};
    r[0] = 0x00;
    r[1] = 0x14;
    put16(&r[2], s.buttons);
    r[4] = s.left_trigger;
    r[5] = s.right_trigger;
    put16(&r[6], std::uint16_t(s.lx));
    put16(&r[8], std::uint16_t(s.ly));
    put16(&r[10], std::uint16_t(s.rx));
    put16(&r[12], std::uint16_t(s.ry));
    return r;
}

std::vector<std::vector<std::uint8_t>> Pad::greeting()
{
    return {{0x01, 0x03, 0x0e}, {0x02, 0x03, 0x00}, {0x03, 0x03, 0x03}, {0x08, 0x03, 0x00}};
}

} // namespace virtpad
