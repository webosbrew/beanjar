#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Grouped VM schema and lossless edits."""

from unittest import mock

import pytest

from beanjar import core, paths, vmconfig, vmidentity, vmsettings


@pytest.fixture
def path(tmp_path):
    return tmp_path / "vm.toml"


def test_defaults(path):
    path.write_text('[firmware]\ndirectory = "firmware"\n')
    cfg = vmconfig.read(path)
    assert cfg == dict(
        smp=4,
        display="dbus",
        audio="dbus",
        link="nat",
        mode="mitm",
        layers=["normal", "gl"],
    )
    vmconfig.write(path, cfg)
    assert path.read_text() == '[firmware]\ndirectory = "firmware"\n'
    assert (
        vmconfig.decode({"firmware": {"directory": "../fw"}, "display": {"backend": "none"}})[
            "audio"
        ]
        == "none"
    )


def test_gpu(path):
    """display.gpu: the launcher's GPU_DEVICE; auto, the default, is not written."""
    path.write_text('[firmware]\ndirectory = "firmware"\n\n[display]\ngpu = "virtio-gpu-gl"\n')
    cfg = vmconfig.read(path)
    assert cfg["gpu"] == "virtio-gpu-gl"
    assert vmsettings.SETTINGS["gpu"] == "GPU_DEVICE"


def test_validation():
    bad = [
        ({"hardware": {"cpus": True}}, "hardware.cpus"),
        ({"hardware": {"memory_mib": 0}}, "hardware.memory_mib"),
        ({"hardware": {"memory": 1024}}, "hardware.memory"),
        ({"display": {"backend": False}}, "display.backend"),
        ({"platform": "webOS"}, "unknown section"),
        ({"advanced": {"env": {"RUN_DIR": "/x"}}}, "owned by the launcher"),
        ({"advanced": {"env": {"PMLOG_DEBUG": True}}}, "string, number"),
        ({"layers": {"enabled": ["../bad"]}}, "layers.enabled"),
        ({"network": {"link": "bridge"}}, "network.link"),
        ({"hardware": {"free_page_reporting": "yes"}}, "hardware.free_page_reporting"),
        ({"display": {"gpu": "on"}}, "display.gpu"),
        ({"display": {"gpu": True}}, "display.gpu"),
        ({"advanced": {"env": {"GPU_DEVICE": "virtio-gpu-gl"}}}, "owned by the launcher"),
        ({"hardware": {"free_page_reporting": True}}, "hardware.free_page_reporting"),
        ({"network": {"mitm_flows": -1}}, "network.mitm_flows"),
        ({"network": {"mitm_flows": "500"}}, "network.mitm_flows"),
        ({"network": {"mitm_flows": True}}, "network.mitm_flows"),
        ({"network": {"mitm_flow_bytes": "lots"}}, "network.mitm_flow_bytes"),
        ({"network": {"mitm_flow_bytes": 200}}, "network.mitm_flow_bytes"),
        ({"network": {"mode": "off"}}, "network.link"),
        ({"network": {"link": "nat", "mode": "off"}}, "network.mode"),
        ({"network": {"net": "mitm"}}, "unknown setting network.net"),
        (
            {"network": {"link": "lan", "listen": "0.0.0.0"}},
            "network.listen: listen is for link=nat",
        ),
        ({"network": {"address": "dhcp"}}, "network.address: address is for link=lan"),
        ({"network": {"link": "off", "address": "dhcp"}}, "address is for link=lan"),
        ({"network": {"link": "lan", "address": "10.0.0.5"}}, "network.address"),
        ({"network": {"link": "lan", "address": "10.0.0.5/30,10.0.0.1"}}, "network.address"),
        ({"network": {"link": "lan", "address": "10.0.0.5/24,10.0.0.5"}}, "network.address"),
        # lan with direct: nothing of Beanjar's in the guest's traffic, so no LG mock, no address
        # of ours and no forwards
        (
            {"network": {"link": "lan", "mode": "direct"}},
            "network.lg_services: link=lan with mode=direct needs lg_services=real",
        ),
        (
            {
                "network": {
                    "link": "lan",
                    "mode": "direct",
                    "lg_services": "real",
                    "address": "dhcp",
                }
            },
            "network.address: address is for link=lan with mode=mitm",
        ),
        (
            {
                "network": {
                    "link": "lan",
                    "mode": "direct",
                    "lg_services": "real",
                    "forward": "8080",
                }
            },
            "network.forward: forward is for link=nat and link=lan with mode=mitm",
        ),
    ]
    for fields, message in bad:
        with pytest.raises(core.Error, match=message):
            vmconfig.decode(dict(firmware={"directory": "firmware"}, **fields))
    with pytest.raises(core.Error):
        vmconfig.decode({})
    with pytest.raises(core.Error, match="unknown section"):
        vmconfig.decode({"smp": 4})


def test_lan_direct():
    """link=lan with mode=direct and lg_services=real is a VM's network, written as given."""
    cfg = vmconfig.decode(
        dict(
            firmware={"directory": "firmware"},
            network={"link": "lan", "mode": "direct", "lg_services": "real"},
        )
    )
    assert (cfg["link"], cfg["mode"], cfg["lg_services"]) == ("lan", "direct", "real")
    assert vmsettings.lan_direct("lan", "direct")
    assert not vmsettings.lan_direct("lan", None)  # mitm, the default
    assert not vmsettings.lan_direct("nat", "direct")
    assert vmsettings.network_label("lan", "direct") == "lan+direct"


def test_userland():
    """hardware.userland: auto, the default, is not written out; native and qemu-user are
    USERLAND; qemu-user is refused on the firmware lines that need native 32-bit execution
    (vmsettings.NATIVE_ONLY)."""
    base = {"firmware": {"directory": "firmware"}}
    assert "userland" not in vmconfig.decode(base)
    assert "USERLAND" not in vmsettings.launcher_env(vmconfig.decode(base))
    assert vmsettings.SETTING_HELP["userland"][1] == "auto"
    cfg = vmconfig.decode(dict(base, hardware={"userland": "qemu-user"}))
    assert vmsettings.launcher_env(cfg)["USERLAND"] == "qemu-user"
    vmsettings.validate(cfg)
    vmsettings.validate(dict(cfg, platform="webOS"))
    vmsettings.validate(dict(cfg, platform="Test"))
    for platform in ("NetCast", "SimpleSmart"):
        with pytest.raises(core.Error, match="userland=qemu-user"):
            vmsettings.validate(dict(cfg, platform=platform))
        vmsettings.validate(dict(cfg, userland="auto", platform=platform))
    vmsettings.validate(dict(cfg, userland="native", platform="NetCast"))
    with pytest.raises(core.Error, match="userland=x86"):
        vmsettings.validate(dict(cfg, userland="x86"))
    assert vmsettings.SETTINGS["userland"] == "USERLAND"


def test_guest_kernel():
    """hardware.guest_kernel: arm64, the default, is not written out; x86-64 is GUEST_KERNEL,
    refused with userland=native and on NetCast and SimpleSmart, and on a host that is not x86-64
    (guest_kernel_host_refusal, at set and at start)."""
    base = {"firmware": {"directory": "firmware"}}
    assert "guest_kernel" not in vmconfig.decode(base)
    assert "GUEST_KERNEL" not in vmsettings.launcher_env(vmconfig.decode(base))
    cfg = vmconfig.decode(dict(base, hardware={"guest_kernel": "x86-64"}))
    assert vmsettings.launcher_env(cfg)["GUEST_KERNEL"] == "x86-64"
    vmsettings.validate(dict(cfg, platform="webOS"))
    vmsettings.validate(dict(cfg, platform="webOS", userland="qemu-user"))
    with pytest.raises(core.Error, match="userland=native is refused") as e:
        vmsettings.validate(dict(cfg, platform="webOS", userland="native"))
    assert e.value.key == "guest_kernel"
    for platform in ("NetCast", "SimpleSmart"):
        with pytest.raises(core.Error, match=f"{platform} firmware needs native 32-bit execution"):
            vmsettings.validate(dict(cfg, platform=platform))
    with pytest.raises(core.Error, match="guest_kernel=arm"):
        vmsettings.validate(dict(cfg, guest_kernel="arm"))
    assert vmsettings.guest_kernel_host_refusal("x86-64", "x86_64") is None
    assert vmsettings.guest_kernel_host_refusal("arm64", "aarch64") is None
    assert "needs an x86-64 host" in vmsettings.guest_kernel_host_refusal("x86-64", "aarch64")


def test_hwdec():
    """hardware.hwdec: auto, the default, is not written out; any other value is MEDIA_HWDEC,
    which advanced.env no longer takes; none refuses MEDIA_SANDBOX=off."""
    base = {"firmware": {"directory": "firmware"}}
    assert "hwdec" not in vmconfig.decode(base)
    assert "MEDIA_HWDEC" not in vmsettings.launcher_env(vmconfig.decode(base))
    for value in ("none", "vaapi", "cuda", "v4l2request", "auto"):
        cfg = vmconfig.decode(dict(base, hardware={"hwdec": value}))
        assert vmsettings.launcher_env(cfg)["MEDIA_HWDEC"] == value
    with pytest.raises(core.Error, match="hardware.hwdec: hwdec=gpu: one of auto, none, cuda"):
        vmconfig.decode(dict(base, hardware={"hwdec": "gpu"}))
    for gone in ("drm", "qsv", "vdpau"):
        with pytest.raises(core.Error, match=f"hwdec={gone}: one of auto, none, cuda, v4l2request"):
            vmconfig.decode(dict(base, hardware={"hwdec": gone}))
    with pytest.raises(core.Error, match="advanced.env.MEDIA_HWDEC is owned by the launcher"):
        vmconfig.decode(dict(base, advanced={"env": {"MEDIA_HWDEC": "none"}}))
    with pytest.raises(core.Error, match="MEDIA_SANDBOX=off is refused") as e:
        vmconfig.decode(
            dict(base, hardware={"hwdec": "none"}, advanced={"env": {"MEDIA_SANDBOX": "off"}})
        )
    assert e.value.key == "hwdec"
    vmconfig.decode(
        dict(base, hardware={"hwdec": "none"}, advanced={"env": {"MEDIA_SANDBOX": "on"}})
    )
    vmconfig.decode(dict(base, advanced={"env": {"MEDIA_SANDBOX": "off"}}))
    assert vmsettings.SETTING_HELP["hwdec"][3] == "auto"  # config unset takes it


def test_configure_hwdec(broken_vm):
    """config set NAME hwdec=none writes hardware.hwdec; auto or an empty value (unset) removes
    it; an unknown device type is refused and nothing is written."""
    lib, toml = broken_vm
    lib.configure("tv", prepare=False, hwdec="none")
    assert 'hwdec = "none"' in toml.read_text()
    assert lib.vm("tv").config["hwdec"] == "none"
    before = toml.read_text()
    with pytest.raises(core.Error, match="hwdec=d3d11va") as e:
        lib.configure("tv", prepare=False, hwdec="d3d11va")
    assert e.value.key == "hwdec"
    assert toml.read_text() == before
    lib.configure("tv", prepare=False, hwdec="")
    assert "hwdec =" not in toml.read_text()
    lib.configure("tv", prepare=False, hwdec="vaapi")
    lib.configure("tv", prepare=False, hwdec="auto")
    assert "hwdec" not in lib.vm("tv").config


@pytest.mark.parametrize(
    "guest_kernel, accel, aarch32, userland, why",
    [
        ("arm64", "tcg", None, "native", "TCG emulates AArch32 itself"),
        ("arm64", "kvm", True, "native", "KVM on a CPU with AArch32 at EL0"),
        ("arm64", "kvm", False, "qemu-user", "KVM on a CPU without AArch32 at EL0"),
        ("arm64", "kvm", None, "native", "KVM on a CPU with AArch32 at EL0"),
        ("arm64", "hvf", False, "qemu-user", "HVF on Apple silicon has no AArch32 at EL0"),
        ("x86-64", "kvm", None, "qemu-user", "the x86-64 guest kernel has no AArch32"),
        ("x86-64", "tcg", None, "qemu-user", "the x86-64 guest kernel has no AArch32"),
    ],
)
def test_resolve_userland(guest_kernel, accel, aarch32, userland, why):
    """userland=auto by host: the guest kernel, the accelerator and whether its CPU has AArch32 at
    EL0; an explicit value is kept."""
    got = vmsettings.resolve_userland("auto", guest_kernel, accel, aarch32, "webOS")
    assert got == (userland, f"userland: {userland} ({why})")
    for setting in ("native", "qemu-user"):
        assert vmsettings.resolve_userland(setting, "arm64", accel, aarch32, "webOS") == (
            setting,
            f"userland: {setting} (as set)",
        )


@pytest.mark.parametrize("platform", ["NetCast", "SimpleSmart"])
def test_resolve_userland_native_only(platform):
    """NetCast and SimpleSmart are a firmware constraint after the host rule: native where the
    host gives it, an error (never qemu-user) where it cannot."""
    for accel, aarch32 in (("tcg", None), ("kvm", True)):
        got = vmsettings.resolve_userland("auto", "arm64", accel, aarch32, platform)
        assert got[0] == "native"
    for guest_kernel, accel, aarch32 in (
        ("arm64", "kvm", False),
        ("arm64", "hvf", None),
        ("x86-64", "kvm", None),
    ):
        with pytest.raises(
            core.Error,
            match=f"{platform} firmware needs native 32-bit execution, which this host cannot "
            "provide",
        ):
            vmsettings.resolve_userland("auto", guest_kernel, accel, aarch32, platform)
    with pytest.raises(core.Error, match="userland=qemu-user"):
        vmsettings.resolve_userland("qemu-user", "arm64", "tcg", None, platform)


def test_free_page_reporting():
    """Opt-in: off is the default and is not written out; on is FREE_PAGE_REPORTING=on."""
    base = {"firmware": {"directory": "firmware"}}
    assert "free_page_reporting" not in vmconfig.decode(base)
    cfg = vmconfig.decode(dict(base, hardware={"free_page_reporting": "on"}))
    assert vmsettings.launcher_env(cfg)["FREE_PAGE_REPORTING"] == "on"
    assert "FREE_PAGE_REPORTING" not in vmsettings.launcher_env(vmconfig.decode(base))


def test_updatable():
    """firmware.updatable: off is the default and is not written out; on is UPDATABLE=on; a value
    other than on/off is refused."""
    base = {"firmware": {"directory": "firmware"}}
    assert "updatable" not in vmconfig.decode(base)
    assert "UPDATABLE" not in vmsettings.launcher_env(vmconfig.decode(base))
    cfg = vmconfig.decode(dict(base, firmware={"directory": "firmware", "updatable": "on"}))
    assert vmsettings.launcher_env(cfg)["UPDATABLE"] == "on"
    for bad, why in (("yes", "one of on, off"), (True, "must be a string")):
        with pytest.raises(core.Error, match=why):
            vmconfig.decode(dict(base, firmware={"directory": "firmware", "updatable": bad}))


def test_lan_access():
    """network.lan_access: off is the default and is not written out; on is NET_LAN_ACCESS=on; a
    value other than on/off is refused, and so is on with link lan (on the LAN anyway)."""
    base = {"firmware": {"directory": "firmware"}}
    assert "lan_access" not in vmconfig.decode(base)
    assert "NET_LAN_ACCESS" not in vmsettings.launcher_env(vmconfig.decode(base))
    cfg = vmconfig.decode(dict(base, network={"lan_access": "on"}))
    assert vmsettings.launcher_env(cfg)["NET_LAN_ACCESS"] == "on"
    vmsettings.validate(cfg)
    for bad, why in (("yes", "one of on, off"), (True, "must be a string")):
        with pytest.raises(core.Error, match=why):
            vmsettings.validate(vmconfig.decode(dict(base, network={"lan_access": bad})))
    with pytest.raises(core.Error, match="lan_access is for link=nat"):
        vmsettings.validate(
            vmconfig.decode(dict(base, network={"link": "lan", "lan_access": "on"}))
        )
    vmsettings.validate(vmconfig.decode(dict(base, network={"link": "off", "lan_access": "on"})))


def test_lg_services():
    """network.lg_services: mock is the default and is not written out (the launcher's own default,
    qemu.py); real is NET_LG_SERVICES=real; any other value is refused."""
    base = {"firmware": {"directory": "firmware"}}
    assert "lg_services" not in vmconfig.decode(base)
    assert "NET_LG_SERVICES" not in vmsettings.launcher_env(vmconfig.decode(base))
    assert vmsettings.DEFAULT_LG_SERVICES == "mock"
    for value in ("mock", "real"):
        cfg = vmconfig.decode(dict(base, network={"lg_services": value}))
        assert vmsettings.launcher_env(cfg)["NET_LG_SERVICES"] == value
        vmsettings.validate(cfg)
    for bad, why in (("off", "one of mock, real"), (False, "must be a string")):
        with pytest.raises(core.Error, match=why):
            vmsettings.validate(vmconfig.decode(dict(base, network={"lg_services": bad})))
    values, default, what, unset = vmsettings.SETTING_HELP["lg_services"]
    assert (values, default, unset) == ("mock|real", "mock", "mock")
    assert "telemetry" in what and "real model key" in what


def test_debug_mode():
    """firmware.debug_mode: debug (the default, not written out), event or release; DEBUG_MODE."""
    base = {"firmware": {"directory": "firmware"}}
    assert "debug_mode" not in vmconfig.decode(base)
    assert "DEBUG_MODE" not in vmsettings.launcher_env(vmconfig.decode(base))
    cfg = vmconfig.decode(dict(base, firmware={"directory": "firmware", "debug_mode": "release"}))
    assert vmsettings.launcher_env(cfg)["DEBUG_MODE"] == "release"
    for bad, why in (("quiet", "one of debug, event, release"), (5, "must be a string")):
        with pytest.raises(core.Error, match=why):
            vmconfig.decode(dict(base, firmware={"directory": "firmware", "debug_mode": bad}))


def test_broadcast(path, tmp_path):
    """firmware.broadcast: the broadcast standard a global firmware boots as (BROADCAST), one of
    dvb, atsc and arib, and one the VM's chip has a board for where its manifest tells the chip."""
    base = {"firmware": {"directory": "firmware"}}
    assert "broadcast" not in vmconfig.decode(base)
    cfg = vmconfig.decode(dict(base, firmware={"directory": "firmware", "broadcast": "atsc"}))
    assert vmsettings.launcher_env(cfg)["BROADCAST"] == "atsc"
    for bad, why in (("pal", "one of dvb, atsc, arib"), (5, "must be a string")):
        with pytest.raises(core.Error, match=why):
            vmconfig.decode(dict(base, firmware={"directory": "firmware", "broadcast": bad}))
    fw = tmp_path / "firmware"
    fw.mkdir()
    manifest = fw / "manifest.toml"
    path.write_text('[firmware]\ndirectory = "firmware"\nbroadcast = "atsc"\n')
    assert vmconfig.read(path)["broadcast"] == "atsc"  # no manifest: the launcher's
    manifest.write_text('name = "x"\nchip = "o22"\nrelease = "queue"\n')
    assert vmconfig.read(path)["broadcast"] == "atsc"
    manifest.write_text('name = "x"\nchip = "lm21ann"\nrelease = "number1"\n')
    with pytest.raises(
        core.Error, match="firmware.broadcast: lm21ann-number1 has no board for broadcast"
    ):
        vmconfig.read(path)
    manifest.write_text('name = "x"\nchip = "o22"\nrelease = "queue"\n')
    (fw / "otncabi-arib.pak").touch()
    assert vmconfig.read(path)["broadcast"] == "atsc"  # pak names state no standard
    # an ARIB firmware: its model_id.json says so
    with mock.patch.object(paths, "model_id", return_value={"region": "ARIB"}):
        with pytest.raises(core.Error, match="broadcast 'atsc', but the firmware is 'arib'"):
            vmconfig.read(path)


def test_model(path, tmp_path):
    """firmware.model: the retail model whose board the VM boots (MODEL), one of its chip's
    model_boards where the manifest tells the chip, of the firmware's and broadcast's standard."""
    base = {"firmware": {"directory": "firmware"}}
    assert "model" not in vmconfig.decode(base)
    assert "MODEL" not in vmsettings.launcher_env(vmconfig.decode(base))
    cfg = vmconfig.decode(dict(base, firmware={"directory": "firmware", "model": "42LX3QPUA"}))
    assert vmsettings.launcher_env(cfg)["MODEL"] == "42LX3QPUA"
    with pytest.raises(core.Error, match="must be a string"):
        vmconfig.decode(dict(base, firmware={"directory": "firmware", "model": 42}))
    fw = tmp_path / "firmware"
    fw.mkdir()
    manifest = fw / "manifest.toml"
    path.write_text('[firmware]\ndirectory = "firmware"\nmodel = "42LX3QPUA"\n')
    assert vmconfig.read(path)["model"] == "42LX3QPUA"  # no manifest: the launcher's
    manifest.write_text('name = "x"\nchip = "o22"\nrelease = "ponytail"\n')
    assert vmconfig.read(path)["model"] == "42LX3QPUA"
    manifest.write_text('name = "x"\nchip = "o22n"\nrelease = "number1"\n')
    with pytest.raises(
        core.Error,
        match="firmware.model: o22n-number1 has no board for model '42LX3QPUA' "
        r"\(it has no model to choose\)",
    ):
        vmconfig.read(path)
    manifest.write_text('name = "x"\nchip = "o22"\nrelease = "ponytail"\n')
    path.write_text('[firmware]\ndirectory = "firmware"\nmodel = "OLED65C2PUA"\n')
    with pytest.raises(core.Error, match=r"no board for model 'OLED65C2PUA' \(it has: 42LX3QPUA\)"):
        vmconfig.read(path)
    # the model's standard is its board's: another broadcast, or firmware, is refused
    path.write_text('[firmware]\ndirectory = "firmware"\nmodel = "42LX3QPUA"\nbroadcast = "dvb"\n')
    with pytest.raises(core.Error, match="model '42LX3QPUA' is a atsc set, not dvb"):
        vmconfig.read(path)
    path.write_text('[firmware]\ndirectory = "firmware"\nmodel = "42LX3QPUA"\n')
    with mock.patch.object(paths, "model_id", return_value={"region": "ARIB"}):
        with pytest.raises(core.Error, match="is a atsc set, but the firmware is 'arib'"):
            vmconfig.read(path)


@pytest.fixture
def broken_vm(tmp_path):
    """A library with a VM `tv` whose in-place firmware (manifest chip lm21u: no broadcast board, and
    an other chip) is in tmp_path/fw; returns (library, vm.toml path)."""
    fw = tmp_path / "fw"
    fw.mkdir()
    (fw / "rootfs.pak").touch()
    (fw / "manifest.toml").write_text('name = "x"\nchip = "lm21u"\nrelease = "kisscurl"\n')
    other = tmp_path / "fw2"
    other.mkdir()
    (other / "rootfs.pak").touch()
    (other / "manifest.toml").write_text('name = "x"\nchip = "o22"\nrelease = "queue"\n')
    lib = core.Library(str(tmp_path / "lib"), repo=str(tmp_path))
    vm_dir = tmp_path / "lib" / "vms" / "tv"
    vm_dir.mkdir(parents=True)
    toml = vm_dir / "vm.toml"
    toml.write_text(f'[firmware]\ndirectory = "{fw}"\n')
    return lib, toml


def test_configure_refuses_bad_broadcast(broken_vm):
    """configure checks broadcast against the firmware before it saves: nothing is written."""
    lib, toml = broken_vm
    before = toml.read_text()
    with pytest.raises(core.Error, match="firmware.broadcast: lm21u-kisscurl has no board") as e:
        lib.configure("tv", prepare=False, broadcast="dvb")
    assert e.value.key == "broadcast"
    assert toml.read_text() == before


def test_configure_model(broken_vm):
    """configure checks model against the firmware before it saves (lm21u has no model to choose);
    on a chip that has the model it is saved in upper case, and an empty value unsets it."""
    lib, toml = broken_vm
    before = toml.read_text()
    with pytest.raises(
        core.Error, match="firmware.model: lm21u-kisscurl has no board for model"
    ) as e:
        lib.configure("tv", prepare=False, model="42LX3QPUA")
    assert e.value.key == "model"
    assert toml.read_text() == before
    other = str(toml.parent.parent.parent.parent / "fw2")
    lib.configure("tv", prepare=False, fw=other, model="42lx3qpua")
    assert lib.vm("tv").config["model"] == "42LX3QPUA"
    lib.configure("tv", prepare=False, model="")
    assert "model" not in lib.vm("tv").config


def test_configure_fixes_saved_bad_broadcast(broken_vm):
    """A vm.toml that already holds a bad broadcast (edited by hand) reads for configure and
    settings, so unset fixes it; other reads still refuse it."""
    lib, toml = broken_vm
    bad = toml.read_text().replace("[firmware]", '[firmware]\nbroadcast = "dvb"')
    toml.write_text(bad)
    with pytest.raises(core.Error, match="has no board for broadcast"):
        lib.vm("tv").config
    assert lib.vm("tv").config_unchecked["broadcast"] == "dvb"
    with pytest.raises(core.Error, match="has no board for broadcast"):
        lib.configure("tv", prepare=False, mem=2048)  # still bad: refused, nothing written
    assert toml.read_text() == bad
    lib.configure("tv", prepare=False, broadcast="")
    assert "broadcast" not in lib.vm("tv").config
    toml.write_text(bad)
    other = str(toml.parent.parent.parent.parent / "fw2")
    lib.configure("tv", prepare=False, fw=other, broadcast="atsc")  # a good value replaces it
    assert lib.vm("tv").config["broadcast"] == "atsc"


def test_configure_refuses_fw_that_breaks_broadcast(broken_vm):
    """Changing fw to a firmware whose chip has no board for the saved broadcast is refused."""
    lib, toml = broken_vm
    other = str(toml.parent.parent.parent.parent / "fw2")
    lib.configure("tv", prepare=False, fw=other, broadcast="dvb")
    before = toml.read_text()
    with pytest.raises(core.Error, match="has no board for broadcast 'dvb'") as e:
        lib.configure("tv", prepare=False, fw=str(toml.parent.parent.parent.parent / "fw"))
    assert e.value.key == "broadcast"
    assert toml.read_text() == before


def test_mitm_flows():
    """The inspector's flow history: 1000 by default, not written out; 0 keeps all."""
    base = {"firmware": {"directory": "firmware"}}
    assert "MITM_FLOWS" not in vmsettings.launcher_env(vmconfig.decode(base))
    for n in (0, 200):
        cfg = vmconfig.decode(dict(base, network={"mitm_flows": n}))
        assert vmsettings.launcher_env(cfg)["MITM_FLOWS"] == str(n)


def test_forward():
    """network.forward is FORWARD; unset, none; not a string, or a bad port, refused."""
    base = {"firmware": {"directory": "firmware"}}
    assert "FORWARD" not in vmsettings.launcher_env(vmconfig.decode(base))
    cfg = vmconfig.decode(dict(base, network={"forward": "8080,8081:80"}))
    assert vmsettings.launcher_env(cfg)["FORWARD"] == "8080,8081:80"
    for bad in (8080, "80", "x"):
        with pytest.raises(core.Error, match="forward"):
            vmconfig.decode(dict(base, network={"forward": bad}))


def test_mitm_flow_bytes():
    """The inspector's byte budget: off by default; a size such as 200M is MITM_FLOW_BYTES."""
    base = {"firmware": {"directory": "firmware"}}
    assert "MITM_FLOW_BYTES" not in vmsettings.launcher_env(vmconfig.decode(base))
    cfg = vmconfig.decode(dict(base, network={"mitm_flow_bytes": "200M"}))
    assert vmsettings.launcher_env(cfg)["MITM_FLOW_BYTES"] == "200M"
    assert [vmsettings.byte_size(x) for x in ("200M", "1g", "512K", "4096", "", "0")] == [
        200 << 20,
        1 << 30,
        512 << 10,
        4096,
        0,
        0,
    ]
    for bad in ("2T", "-1", "1.5M", "M"):
        with pytest.raises(ValueError):
            vmsettings.byte_size(bad)
    assert (vmsettings.flow_bytes_label("200M"), vmsettings.flow_bytes_label(None)) == (
        "200 MiB",
        "no limit",
    )


def test_network():
    base = {"firmware": {"directory": "firmware"}}
    cfg = vmconfig.decode(
        dict(
            base,
            network={
                "link": "lan",
                "mode": "mitm",
                "address": "192.168.1.50/24,192.168.1.1",
            },
        )
    )
    assert (cfg["link"], cfg["mode"], cfg["address"]) == (
        "lan",
        "mitm",
        "192.168.1.50/24,192.168.1.1",
    )
    assert (
        vmconfig.decode(dict(base, network={"link": "lan", "address": "dhcp"}))["address"] == "dhcp"
    )
    assert vmconfig.decode(dict(base, network={"link": "off", "mode": "direct"}))["link"] == "off"
    assert (
        vmconfig.decode(dict(base, network={"link": "nat", "listen": "0.0.0.0"}))["listen"]
        == "0.0.0.0"
    )
    assert vmsettings.launcher_env(dict(link="lan", mode="mitm", address="dhcp")) == {
        "NET_LINK": "lan",
        "NET_MODE": "mitm",
        "NET_ADDRESS": "dhcp",
    }


def test_lan_parent(tmp_path):
    sys_net = tmp_path / "net"
    for name in ("eth0", "wlan0/wireless", "lo"):
        (sys_net / name).mkdir(parents=True)
    assert vmidentity.parse_lan_parent(" eth0 ", str(sys_net)) == "eth0"
    for value, message in (
        ("wlan0", "wireless"),
        ("eth1", "no such"),
        ("lo", "loopback"),
        ("../eth0", "interface name"),
        ("a" * 16, "interface name"),
    ):
        with pytest.raises(core.Error, match=message):
            vmidentity.parse_lan_parent(value, str(sys_net))


def test_edit_and_failed_edit(path):
    original = '# user note\n[firmware]\ndirectory="../firmware" # location\n\n[hardware] # machine\ncpus=4 # keep this\n\n[advanced]\nenv = { PMLOG_DEBUG = "1" } # debugging\n'
    path.write_text(original)
    cfg = vmconfig.read(path)
    vmconfig.write(path, cfg)
    assert path.read_text() == original
    cfg["smp"] = 2
    vmconfig.write(path, cfg)
    expected = original.replace("cpus=4", "cpus=2")
    assert path.read_text() == expected
    cfg["smp"] = True
    with pytest.raises(core.Error):
        vmconfig.write(path, cfg)
    assert path.read_text() == expected
