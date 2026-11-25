// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
// The camera's configuration descriptor read back as data (the interfaces and endpoints of each
// alternate setting, the video formats and frames), and the probe/commit negotiation that follows
// from it. No usbipdcpp and no I/O: the unit tests drive these directly.
#pragma once

#include <cstddef>
#include <cstdint>
#include <optional>
#include <vector>

namespace virtcam
{

struct Endpoint {
    std::uint8_t address = 0;
    std::uint8_t attributes = 0;
    std::uint16_t max_packet_size = 0; // wMaxPacketSize as sent (bits 12:11: extra transactions)
    std::uint8_t interval = 0;

    // bytes per (micro)frame: (wMaxPacketSize & 0x7ff) * (1 + bits 12:11)
    std::uint32_t payload() const
    {
        return std::uint32_t(max_packet_size & 0x7ff) * (1 + ((max_packet_size >> 11) & 3));
    }
};

struct AltSetting {
    std::uint8_t alt = 0;
    std::uint8_t cls = 0, subclass = 0, protocol = 0;
    std::vector<Endpoint> endpoints;
};

struct Interface {
    std::uint8_t number = 0;
    std::vector<AltSetting> alts; // by bAlternateSetting, 0 first
};

struct Frame {
    std::uint8_t index = 0;
    std::uint16_t width = 0, height = 0;
    std::uint32_t max_frame_size = 0;     // dwMaxVideoFrameBufferSize (frame-based: width*height*2)
    std::uint32_t default_interval = 0;   // 100 ns units
    std::vector<std::uint32_t> intervals; // discrete, as listed; continuous: {min, max, step}
    bool continuous = false;
};

enum class Kind { Uncompressed, Mjpeg, FrameBased };

struct Format {
    std::uint8_t index = 0;
    Kind kind = Kind::Uncompressed;
    std::uint32_t fourcc = 0; // the GUID's first four bytes (YUY2, H264), MJPG for MJPEG
    std::uint8_t default_frame = 1;
    std::vector<Frame> frames;

    const Frame *frame(std::uint8_t index) const;
};

constexpr std::uint32_t fourcc(char a, char b, char c, char d)
{
    return std::uint32_t(std::uint8_t(a)) | std::uint32_t(std::uint8_t(b)) << 8 |
           std::uint32_t(std::uint8_t(c)) << 16 | std::uint32_t(std::uint8_t(d)) << 24;
}
constexpr std::uint32_t FOURCC_YUY2 = fourcc('Y', 'U', 'Y', '2');
constexpr std::uint32_t FOURCC_MJPG = fourcc('M', 'J', 'P', 'G');
constexpr std::uint32_t FOURCC_H264 = fourcc('H', '2', '6', '4');

struct Config {
    std::uint8_t configuration_value = 1;
    std::vector<Interface> interfaces;        // in descriptor order
    std::optional<std::uint8_t> vs_interface; // the (first) VideoStreaming interface
    std::uint8_t processing_unit = 0; // the VideoControl interface's bUnitID of it (0: none)
    std::vector<Format> formats;

    const Interface *interface(std::uint8_t number) const;
    const Format *format(std::uint8_t index) const;
    // the isochronous IN endpoint of the VS interface's alternate setting alt (nullptr: none)
    const Endpoint *alt_endpoint(std::uint8_t alt) const;
    // payload bytes per packet of the VS interface's alternate setting alt (0: none)
    std::uint32_t alt_payload(std::uint8_t alt) const;
    // among the VS alternate settings that send every microframe (bInterval 1, as all the
    // C920's do): the lowest whose payload is at least bytes (as uvcvideo picks it); the largest
    // when none is
    std::uint8_t alt_for(std::uint32_t bytes) const;
    // for a compressed stream of bytes_per_second: the alternate setting that sends less often
    // than every microframe (bInterval above 1: ours, not the C920's) with the least bandwidth
    // that carries it (packets per second x payload less the 2-byte header); 0 when none does
    std::uint8_t sparse_alt_for(std::uint64_t bytes_per_second) const;
};

// Reads a whole configuration descriptor (wTotalLength bytes); nullopt if it is malformed.
std::optional<Config> parse_config(const std::uint8_t *data, std::size_t size);

// The video probe and commit control (UVC 1.1 Table 4-47), its first 26 bytes: the UVC 1.0 size,
// which a bcdUVC 1.00 camera uses (34 or 48 with the later fields for 1.1 and 1.5).
// A compressed frame's size as negotiate() budgets it: 0.75 bits per pixel. jpegenc's quality-85
// baseline JPEG of the smpte pattern (with its time overlay) is 0.69 at 640x480, 0.57 at 1280x720
// and 0.51 at 1920x1080, so 1280x720 at 30 fps (2.07 MB/s) fits in 3066 bytes x 1000 packets/s and
// 1920x1080 at 30 fps (5.8 MB/s) in 3072 bytes x 2000 packets/s.
constexpr std::uint32_t COMPRESSED_BITS_PER_PIXEL_X4 = 3;

struct Probe {
    static constexpr std::size_t SIZE = 26;
    std::uint16_t hint = 0;
    std::uint8_t format = 0, frame = 0;
    std::uint32_t interval = 0;
    std::uint16_t key_frame_rate = 0, p_frame_rate = 0, comp_quality = 0, comp_window = 0;
    std::uint16_t delay = 0;
    std::uint32_t max_video_frame_size = 0;
    std::uint32_t max_payload = 0;

    static Probe read(const std::uint8_t *data, std::size_t size);
    std::vector<std::uint8_t> bytes() const;
};

// What the camera answers to a probe: a format and frame it has (the default where the request
// names none or one it lacks), the listed frame interval nearest the request (the frame's default
// for 0), the frame's buffer size, and the payload per packet this rate needs, rounded up to an
// alternate setting's (uncompressed: frame bytes x frame rate / 8000 plus the 2-byte header;
// compressed: the sparse alternate setting with the least bandwidth that carries
// COMPRESSED_BITS_PER_PIXEL_X4 / 4 at this size and rate, else the largest one, since the size of
// a frame varies).
Probe negotiate(const Config &config, const Probe &request);
// GET_DEF: the first format's default frame at its default interval.
Probe default_probe(const Config &config);

// usbipdcpp's payload header marks the end of a frame with bit 7 (UVC's EOH); UVC and Linux
// uvcvideo take EOF from bit 1. Moves it, in a packet's header (data, its length).
void fix_payload_header(std::uint8_t *data, std::size_t size);

} // namespace virtcam
