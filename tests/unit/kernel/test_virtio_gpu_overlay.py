# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""virtio-gpu's overlay plane (kernel patch 0028): only with virtio_gpu.overlay_plane=1 does a CRTC
get one, with the primary's formats, placed between the primary and the cursor; it is scanned out
only while the primary has no framebuffer, and the primary takes the scanout back. Spacebuns' gl
layer is the one that sets the word (docs/research/graphics/compositors.md, "Spacebuns outputs on a
set"); what it shows is checked in a booted guest."""

import re
from pathlib import Path

import pytest
from linuxsrc import linux_source

TOP = Path(__file__).resolve().parents[3]
PATCH = "0028-virtio-gpu-overlay-plane.patch"
VIRTIO = Path(linux_source()) / "drivers/gpu/drm/virtio"
APPLIED = (VIRTIO / "virtgpu_plane.c").is_file() and "virtio_gpu_overlay_plane_update" in (
    VIRTIO / "virtgpu_plane.c"
).read_text()


def body(text, name):
    m = re.search(r"^static (?:void|int) " + name + r"\(.*?^\}", text, re.M | re.S)
    assert m, name
    return m.group(0)


def test_patch_is_applied_by_the_wrap_and_only_spacebuns_may_set_it():
    assert (TOP / "guest/kernel/patches" / PATCH).is_file()
    assert "linux/" + PATCH in (TOP / "subprojects/linux.wrap").read_text()
    words = []
    for path in (TOP / "guest/layers").rglob("kernel.cmdline"):
        if "virtio_gpu.overlay_plane=1" in path.read_text():
            words.append(path.relative_to(TOP).as_posix())
    # no layer but spacebuns' native gl tree asks for it
    assert set(words) <= {"guest/layers/gl/native-tv-rootfs/usr/share/beanjar/kernel.cmdline"}


@pytest.mark.skipif(not APPLIED, reason=f"no unpacked kernel source with {PATCH}: {VIRTIO}")
def test_overlay_plane_is_behind_the_parameter():
    display = (VIRTIO / "virtgpu_display.c").read_text()
    init = re.search(r"^static int vgdev_output_init\(.*?^\}", display, re.M | re.S).group(0)
    overlay = init.index("if (virtio_gpu_overlay_plane)")
    assert init.index("DRM_PLANE_TYPE_PRIMARY") < overlay < init.index("DRM_PLANE_TYPE_CURSOR")
    assert "bool virtio_gpu_overlay_plane;" in (VIRTIO / "virtgpu_drv.c").read_text()


@pytest.mark.skipif(not APPLIED, reason=f"no unpacked kernel source with {PATCH}: {VIRTIO}")
def test_overlay_scans_out_only_without_the_primary():
    plane = (VIRTIO / "virtgpu_plane.c").read_text()
    update = body(plane, "virtio_gpu_overlay_plane_update")
    assert "output->crtc.primary->state->fb" in update
    primary = body(plane, "virtio_gpu_primary_plane_update")
    # the primary's nofb path hands the scanout to an overlay with a framebuffer
    assert "virtio_gpu_overlay_show(output->overlay, NULL)" in primary
    # and the primary takes it back when it has one again
    assert "output->overlay_shown) {" in primary
    # overlays may be positioned and clipped, as the cursor; the primary still covers the CRTC
    check = body(plane, "virtio_gpu_plane_atomic_check")
    assert "can_position = plane->type != DRM_PLANE_TYPE_PRIMARY" in check
    # the overlay has the primary's formats
    init = re.search(r"^struct drm_plane \*virtio_gpu_plane_init\(.*?^\}", plane, re.M | re.S)
    assert "formats = virtio_gpu_formats;" in init.group(0)
