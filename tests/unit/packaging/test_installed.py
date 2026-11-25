#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Installed resources stay immutable; generated data belongs to the user."""

from pathlib import Path
import importlib.util
import os
import subprocess
import sys
from unittest import mock

import pytest

TOP = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(TOP / "host/core"))
from beanjar import paths, lls, share


def test_paths_and_prebuilt_probe(tmp_path):
    root = tmp_path
    tree = paths.Tree(root)
    assert tree.cache("lls") == str(root / "build/lls")
    (root / "installation.toml").write_text('version="test"\n')
    with mock.patch.dict(os.environ, XDG_CACHE_HOME=str(root / "user-cache")):
        assert tree.cache("lls") == str(root / "user-cache/beanjar/lls")
        assert tree.build("lls", "lls-probe") == "/usr/share/beanjar/guest/bin/lls-probe"
        with mock.patch("os.path.isfile", return_value=True):
            assert lls.probe(tree) == "/usr/share/beanjar/guest/bin/lls-probe"
            assert lls.probe(tree, preload=True) == "/usr/share/beanjar/guest/bin/lls-probe.so"
    assert tree.build("Image") == "/usr/share/beanjar/guest/Image"
    assert tree.build("vnc", "beanjar-vnc") == "/usr/bin/beanjar-vnc"
    assert (
        tree.src("guest/layers/soc/lm21a/rootfs/etc/emulator/board.cmdline")
        == "/usr/share/beanjar/guest/boards/lm21a/board.cmdline"
    )
    assert (
        tree.src("guest/layers/soc/o22/rootfs/etc/emulator/board-arib.cmdline")
        == "/usr/share/beanjar/guest/boards/o22/board-arib.cmdline"
    )
    # a board source other than a soc layer (targets.Soc.board_src): at its checkout path
    assert (
        tree.src("guest/layers/netcast45/rootfs/etc/emulator/board.cmdline")
        == "/usr/share/beanjar/guest/boards/guest/layers/netcast45/rootfs/etc/emulator/board.cmdline"
    )
    # the board definitions, beanjar-guest's (packaging/debian/build.py's stage_targets)
    assert (
        tree.src("guest", "targets", "soc", "k8hp.toml")
        == "/usr/share/beanjar/guest/targets/soc/k8hp.toml"
    )
    assert tree.src("host/helpers/network/tlsca.py") == str(root / "helpers/network/tlsca.py")
    assert tree.pkg_parent == str(root / "core")


def test_share_uses_guest_data_and_refuses_escaping_links(tmp_path):
    root = tmp_path
    library = root / "lib"
    library.mkdir()
    (library / "installation.toml").write_text('version="test"')
    tree = paths.Tree(library)
    guest = root / "share/guest"
    (guest / "scripts").mkdir(parents=True)
    file = guest / "scripts/save-logs.sh"
    file.write_text("runtime script")

    def source(path):
        return str(guest if path == "guest" else guest / path.removeprefix("guest/"))

    with mock.patch.object(tree, "src", side_effect=source):
        assert share._checkout_rel(tree, "guest/scripts/save-logs.sh") == (
            "guest/scripts/save-logs.sh",
            str(file),
        )
        file.unlink()
        file.symlink_to(root / "private")
        with pytest.raises(share.Error, match="not a packaged"):
            share._checkout_rel(tree, "guest/scripts/save-logs.sh")


def test_package_depends_on_vendored_or_system_qemu():
    spec = importlib.util.spec_from_file_location("debian_build", TOP / "packaging/debian/build.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    groups = [
        group.strip()
        for group in build.host_dependencies("0.2.1", "libc6", build.load_identity()).split(",")
    ]
    assert groups[:2] == ["beanjar-guest (>= 0.2)", "beanjar-guest (<< 0.3)"], (
        "any guest of the host's contract, never one pinned to the host version"
    )
    assert "beanjar-qemu | qemu-system-arm" in groups
    assert "beanjar-qemu | qemu-system-modules-opengl" in groups
    assert "qemu-system-arm" not in groups, "the system QEMU only as the alternative"
    assert not (TOP / "packaging/debian/build-deps/system-qemu.control").exists()


def test_vnc_package_depends_on_the_same_beanjar():
    spec = importlib.util.spec_from_file_location("debian_build", TOP / "packaging/debian/build.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    groups = [group.strip() for group in build.vnc_dependencies("1.2", "libneatvnc1").split(",")]
    assert groups == ["beanjar (= 1.2)", "libneatvnc1"]


def test_web_package(tmp_path):
    """beanjar-web: the same beanjar, beanjar-vnc, aiohttp and Debian's noVNC; its tree holds
    the package and a launcher that imports it from /usr/lib/beanjar/web."""
    spec = importlib.util.spec_from_file_location("debian_build", TOP / "packaging/debian/build.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    groups = [group.strip() for group in build.web_dependencies("1.2").split(",")]
    assert groups[:2] == ["beanjar (= 1.2)", "beanjar-vnc"]
    assert "python3-aiohttp" in groups
    assert "novnc" in groups
    assert "bootstrap-icons" in groups
    work = str(tmp_path)
    web = build.stage_web(Path(work))
    package = web / "usr/lib/beanjar/web/beanjar_web"
    assert (package / "server.py").is_file()
    assert (package / "static/remote.js").is_file()
    launcher = (web / "usr/bin/beanjar-web").read_text()
    assert "/usr/lib/beanjar/web" in launcher
    assert launcher.startswith(f"#!/usr/bin/python3\n{build.PYCACHE}")


def test_ui_package(tmp_path):
    """beanjar-ui: the same beanjar and GTK for Python, which beanjar itself does not need; its
    tree holds beanjar_ui, the beanjar-ui launcher (the core's package on its path too), both
    bundles and the desktop entry."""
    spec = importlib.util.spec_from_file_location("debian_build", TOP / "packaging/debian/build.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    groups = [group.strip() for group in build.ui_dependencies("1.2").split(",")]
    assert groups[0] == "beanjar (= 1.2)"
    assert "gir1.2-gtk-4.0 (>= 4.12)" in groups
    host = build.host_dependencies("1.2.0", "libc6", build.load_identity())
    assert "gtk" not in host
    assert "adw" not in host
    work = str(tmp_path)
    publish = Path(work) / "publish"
    for bundle in ("manager/beanjar.gresource", "viewer/beanjar-viewer.gresource"):
        (publish / bundle).parent.mkdir(parents=True, exist_ok=True)
        (publish / bundle).write_bytes(b"")
    ui = build.stage_ui(Path(work), publish)
    package = ui / "usr/lib/beanjar/ui/beanjar_ui"
    assert (package / "manager/gui.py").is_file()
    assert (package / "viewer/main.py").is_file()
    launcher = (ui / "usr/bin/beanjar-ui").read_text()
    assert launcher.startswith(f"#!/usr/bin/python3\n{build.PYCACHE}")
    assert 'sys.path.insert(0,"/usr/lib/beanjar/ui")' in launcher
    assert 'sys.path.insert(1,"/usr/lib/beanjar/core")' in launcher
    assert "from beanjar_ui.manager.gui import main" in launcher
    assert (ui / "usr/share/beanjar/ui/beanjar.gresource").is_file()
    desktop = (ui / "usr/share/applications/org.webosbrew.beanjar.desktop").read_text()
    assert "Exec=beanjar-ui\n" in desktop


def test_launcher_bytecode_cache(tmp_path):
    """The launchers keep bytecode in $XDG_CACHE_HOME/beanjar/pycache (default ~/.cache),
    owner-only, never beside the installed sources; without a directory of this user's own
    there, none is written."""
    spec = importlib.util.spec_from_file_location("debian_build", TOP / "packaging/debian/build.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    code = build.PYCACHE + (
        "sys.path.insert(0, sys.argv[1])\nimport probe_mod\n"
        "print(sys.pycache_prefix, sys.dont_write_bytecode)\n"
    )
    root = tmp_path
    lib = root / "lib"
    lib.mkdir()
    (lib / "probe_mod.py").write_text("X = 1\n")

    def run(**env):
        environ = {k: v for k, v in os.environ.items() if k != "XDG_CACHE_HOME"}
        result = subprocess.run(
            [sys.executable, "-I", "-c", code, str(lib)],
            env={**environ, **env},
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.split()

    cache = root / "cache/beanjar/pycache"
    assert run(XDG_CACHE_HOME=str(root / "cache")) == [str(cache), "False"]
    assert cache.stat().st_mode & 0o777 == 0o700
    assert list(cache.rglob("probe_mod.*.pyc")), "bytecode in the cache"
    assert not (lib / "__pycache__").exists(), "none beside the sources"
    home = root / "home"
    home.mkdir()
    got = run(XDG_CACHE_HOME="relative", HOME=str(home))
    assert got == [str(home / ".cache/beanjar/pycache"), "False"]
    (root / "file").write_text("")
    assert run(XDG_CACHE_HOME=str(root / "file")) == ["None", "True"]
    if os.geteuid() != 0:  # the nearest existing directory, /, is root's
        assert run(XDG_CACHE_HOME="/") == ["None", "True"]
    assert not (lib / "__pycache__").exists()


def test_shlibs_file_takes_dpkg_shlibdeps_output():
    spec = importlib.util.spec_from_file_location("debian_build", TOP / "packaging/debian/build.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    libs = "libc6 (>= 2.38), libgtk-4-1 (>= 4.12)"
    for text in (
        f"shlibs:Depends={libs}\n",
        f"shlibs:Depends={libs}\nmisc:Depends=\n",
        f"{libs}\n",
    ):
        assert build.shlibs_depends(text) == libs
    for text in ("", "misc:Depends=foo\n", "shlibs:Depends=\n", "a\nb\n"):
        with pytest.raises(SystemExit, match="shlibs:Depends="):
            build.shlibs_depends(text)


def _build_module():
    spec = importlib.util.spec_from_file_location("debian_build", TOP / "packaging/debian/build.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    return build


def test_installed_x86_64_guest_kernel_paths(tmp_path):
    """An installed host finds the experimental x86-64 guest kernel's pieces where the packages
    put them: the kernel and initramfs in beanjar-guest-x86-64's x86_64/, qemu-system-x86_64 beside
    qemu-system-aarch64 with -L the shared QEMU data, and each guest kernel's qemu-arm in
    beanjar-qemu's qemu-user/<guest architecture>/; no environment override needed."""
    tree = paths.Tree(tmp_path)
    (tmp_path / "installation.toml").write_text('version="test"\n')
    assert tree.guest_kernel("x86-64") == (
        "/usr/share/beanjar/guest/x86_64/bzImage",
        "/usr/share/beanjar/guest/x86_64/initramfs.cpio.gz",
    )
    assert tree.guest_kernel("arm64") == (
        "/usr/share/beanjar/guest/Image",
        "/usr/share/beanjar/guest/initramfs.cpio.gz",
    )
    assert tree.vendored_qemu_x86_64() == "/usr/libexec/beanjar/qemu-system-x86_64"
    real_isfile = os.path.isfile
    present = {
        "/usr/libexec/beanjar/qemu-system-x86_64",
        "/usr/share/beanjar/qemu-user/arm64/qemu-arm",
        "/usr/share/beanjar/qemu-user/amd64/qemu-arm",
    }
    with (
        mock.patch("os.path.isfile", side_effect=lambda p: p in present or real_isfile(p)),
        mock.patch("os.access", return_value=True),
    ):
        assert tree.qemu_x86_64({}) == (
            "/usr/libexec/beanjar/qemu-system-x86_64",
            ["-L", "/usr/share/beanjar/qemu"],
        )
        assert tree.qemu_arm({}, "x86-64") == "/usr/share/beanjar/qemu-user/amd64/qemu-arm"
        assert tree.qemu_arm({}, "arm64") == "/usr/share/beanjar/qemu-user/arm64/qemu-arm"
        assert tree.qemu_arm({"QEMU_ARM": str(tmp_path / "installation.toml")}, "x86-64") == str(
            tmp_path / "installation.toml"
        )
    with mock.patch("os.path.isfile", side_effect=lambda p: p not in present and real_isfile(p)):
        assert tree.qemu_x86_64({}) == (None, [])
        assert tree.qemu_arm({}, "x86-64") is None


def _elf(path, arch):
    """A file whose ELF header names arch's e_machine (all build.elf_arch reads)."""
    machine = {"amd64": 62, "arm64": 183}[arch]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x7fELF" + bytes(14) + machine.to_bytes(2, "little") + bytes(44))


def _vendor_tree(root, program, arch, binaries, version="10.0.2+beanjar2", depends=None):
    for binary in binaries:
        _elf(root / binary, arch)
    (root / "share/doc" / program).mkdir(parents=True)
    (root / "share/doc" / program / "COPYING").write_text("GPL\n")
    info = f"Program: {program}\nVersion: {version}\nArchitecture: {arch}\n"
    if depends is not None:
        info += f"Depends: {depends}\n"
    (root / "BUILDINFO").write_text(info)


def _publish(tmp_path, arch, x86_64=True, qemu_user=("arm64", "amd64")):
    hp = tmp_path / "publish"
    binaries = ["bin/qemu-system-aarch64", "libexec/vhost-user-gpu"]
    if x86_64:
        binaries.append("bin/qemu-system-x86_64")
    _vendor_tree(hp / "qemu", "qemu", arch, binaries, depends="libc6 (>= 2.38)")
    (hp / "qemu/share/qemu").mkdir(parents=True)
    (hp / "qemu/share/qemu/bios-256k.bin").write_bytes(b"bios")
    for guest in qemu_user:
        directory = "qemu-user" if guest == "arm64" else f"qemu-user-{guest}"
        programs = ["bin/qemu-arm"] + (["bin/qemu-aarch64"] if guest == "amd64" else [])
        _vendor_tree(hp / directory, "qemu-user", guest, programs, "10.0.2+beanjar1")
    return hp


def _files(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def test_qemu_package_amd64_ships_the_x86_64_guest_kernel_programs(tmp_path):
    """beanjar-qemu on amd64: qemu-system-x86_64 beside qemu-system-aarch64, and the qemu-arm of
    both guest kernels, each with its licence texts, BUILDINFO and a copyright stanza."""
    build = _build_module()
    hp = _publish(tmp_path, "amd64")
    q, depends = build.stage_qemu(tmp_path, hp, "amd64")
    files = _files(q)
    for path in (
        "usr/libexec/beanjar/qemu-system-aarch64",
        "usr/libexec/beanjar/qemu-system-x86_64",
        "usr/libexec/beanjar/vhost-user-gpu",
        "usr/share/beanjar/qemu/bios-256k.bin",
        "usr/share/beanjar/qemu-user/arm64/qemu-arm",
        "usr/share/beanjar/qemu-user/amd64/qemu-arm",
        "usr/share/beanjar/qemu-user/amd64/qemu-aarch64",
        "usr/share/doc/beanjar-qemu/qemu-user/arm64/BUILDINFO",
        "usr/share/doc/beanjar-qemu/qemu-user/amd64/COPYING",
    ):
        assert path in files
    assert build.elf_arch(q / "usr/share/beanjar/qemu-user/arm64/qemu-arm") == "arm64"
    copyright = (q / "usr/share/doc/beanjar-qemu/copyright").read_text()
    assert "Files: usr/share/beanjar/qemu-user/amd64/*" in copyright
    assert "release qemu-user-10.0.2+beanjar1" in copyright
    assert depends == "libc6 (>= 2.38)"


def test_qemu_package_arm64_carries_only_arm64_qemu_user(tmp_path):
    """beanjar-qemu on arm64: no x86-64 program, even where a stale amd64 qemu-user tree or a
    qemu-system-x86_64 is in the publish directory; its qemu-user is the arm64 guest kernel's."""
    build = _build_module()
    hp = _publish(tmp_path, "arm64")
    q, _ = build.stage_qemu(tmp_path, hp, "arm64")
    files = _files(q)
    assert "usr/share/beanjar/qemu-user/arm64/qemu-arm" in files
    assert not any("x86_64" in f or "/amd64" in f for f in files), files


def test_qemu_package_without_the_unreleased_parts(tmp_path):
    """A QEMU tree without qemu-system-x86_64 and no qemu-user published (the pins of before):
    beanjar-qemu as it was."""
    build = _build_module()
    hp = _publish(tmp_path, "amd64", x86_64=False, qemu_user=())
    q, _ = build.stage_qemu(tmp_path, hp, "amd64")
    files = _files(q)
    assert "usr/libexec/beanjar/qemu-system-aarch64" in files
    assert not any("x86_64" in f or "qemu-user" in f for f in files), files


def test_qemu_package_refuses_a_qemu_user_of_another_architecture(tmp_path):
    build = _build_module()
    hp = _publish(tmp_path, "amd64", qemu_user=("arm64",))
    _vendor_tree(hp / "qemu-user-amd64", "qemu-user", "arm64", ["bin/qemu-arm"])
    with pytest.raises(SystemExit, match="qemu-user for amd64"):
        build.stage_qemu(tmp_path, hp, "amd64")


def test_guest_package_has_no_x86_64_kernel_and_its_own_package_has(tmp_path):
    """beanjar-guest carries only the arm64 kernel; the x86-64 kernel and initramfs are
    beanjar-guest-x86-64's, at the paths the launcher resolves, and it is not built where the guest
    lane published no x86-64 kernel."""
    build = _build_module()
    gp = tmp_path / "publish"
    for name in ("Image", "initramfs.cpio.gz", "x86_64/bzImage", "x86_64/initramfs.cpio.gz"):
        (gp / name).parent.mkdir(parents=True, exist_ok=True)
        (gp / name).write_bytes(name.encode())
    g = tmp_path / "guest"
    build.stage_kernels(gp, g)
    assert _files(g) == ["Image", "initramfs.cpio.gz"]
    x = build.stage_x86_64(tmp_path, gp)
    base = x / "usr/share/beanjar/guest"
    assert _files(base) == ["x86_64/bzImage", "x86_64/initramfs.cpio.gz"]
    tree = paths.Tree(tmp_path)
    (tmp_path / "installation.toml").write_text('version="test"\n')
    for path in tree.guest_kernel("x86-64"):
        assert (base / os.path.relpath(path, "/usr/share/beanjar/guest")).is_file()
    for name in ("x86_64/bzImage", "x86_64/initramfs.cpio.gz"):
        (gp / name).unlink()
    assert build.stage_x86_64(tmp_path / "other", gp) is None


def test_x86_64_package_control(tmp_path):
    build = _build_module()
    root = tmp_path / "pkg"
    (root / "usr/share/beanjar/guest/x86_64").mkdir(parents=True)
    build.control(
        root,
        "beanjar-guest-x86-64",
        "0.2.3",
        "amd64",
        "beanjar-guest (= 0.2.3), beanjar-qemu",
        build.X86_64_DESCRIPTION,
    )
    text = (root / "DEBIAN/control").read_text()
    fields = dict(line.split(": ", 1) for line in text.splitlines() if not line.startswith(" "))
    assert fields["Package"] == "beanjar-guest-x86-64"
    assert fields["Architecture"] == "amd64"
    assert fields["Depends"] == "beanjar-guest (= 0.2.3), beanjar-qemu"
    assert "Recommends" not in fields and "Suggests" not in fields
    assert "experimental x86-64 guest kernel" in fields["Description"]
    assert "ARM webOS" in text


def test_host_suggests_but_never_recommends_the_x86_64_package(tmp_path):
    build = _build_module()
    root = tmp_path / "pkg"
    root.mkdir()
    build.control(root, "beanjar", "0.2.3", "amd64", "x", "d", suggests="beanjar-guest-x86-64")
    text = (root / "DEBIAN/control").read_text()
    assert "Suggests: beanjar-guest-x86-64\n" in text
    assert "Recommends" not in text


def test_x86_64_kernel_refused_when_installed_without_its_package(tmp_path):
    from beanjar import vmsettings

    tree = paths.Tree(tmp_path)
    assert vmsettings.guest_kernel_package_refusal("x86-64", tree) is None  # a checkout
    (tmp_path / "installation.toml").write_text('version="test"\n')
    refusal = vmsettings.guest_kernel_package_refusal("x86-64", tree)
    assert "beanjar-guest-x86-64" in refusal
    assert vmsettings.guest_kernel_package_refusal("arm64", tree) is None
    with mock.patch("os.path.isfile", return_value=True):
        assert vmsettings.guest_kernel_package_refusal("x86-64", tree) is None


def test_board_definitions_ship_with_the_guest(tmp_path):
    """The board definitions are beanjar-guest's (/usr/share/beanjar/guest/targets, where
    paths.Tree.src finds them), without what only the guest build reads (meson.build, facts/);
    the beanjar package carries none."""
    spec = importlib.util.spec_from_file_location("debian_build", TOP / "packaging/debian/build.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    g = tmp_path / "beanjar-guest/usr/share/beanjar/guest"
    build.stage_targets(g)
    shipped = sorted(str(p.relative_to(g / "targets")) for p in (g / "targets").rglob("*.toml"))
    source = TOP / "guest/targets"
    assert shipped == sorted(
        str(p.relative_to(source)) for p in source.rglob("*.toml") if p.parts[-2] != "facts"
    )
    assert "soc/k8hp.toml" in shipped
    assert not (g / "targets/meson.build").exists()
    assert not (g / "targets/facts").exists()

    # the host package, staged from a stand-in publish directory (its native artifacts and prebuilt
    # trees are not there, so only the source tree's files are copied): no board definitions
    hp, arch = tmp_path / "publish", "amd64"
    for program in ("ffmpeg", "neatvnc"):
        (hp / program / "share/doc").mkdir(parents=True)
    copy, sources = build.copy, []

    def source_only(src, dst):
        sources.append(src)
        if not src.is_relative_to(hp):
            copy(src, dst)

    with (
        mock.patch.object(build, "copy", side_effect=source_only),
        mock.patch.object(build, "elf_arch", return_value=arch),
        mock.patch.object(build, "vendor_tree", side_effect=lambda hp, p, a: (hp / p, {})),
        mock.patch.object(build, "stage_libraries"),
        mock.patch.object(build, "vendor_licence", return_value=""),
    ):
        host, _ = build.stage_host(tmp_path / "work", hp, arch, {"version": "1", "commit": "c"})
    assert TOP / "host/core/beanjar" in sources
    assert not any(src.is_relative_to(source) for src in sources)
    assert not list(host.rglob("targets"))


def test_every_target_board_file_ships_where_the_installed_host_reads_it(tmp_path):
    """beanjar-guest's staging puts every target's board file (targets.board_cmdline_src, for its
    default board and each of its broadcast boards, whichever layer is its board source) where an
    installed paths.Tree.src finds it, each source's file at its own path."""
    from beanjar import targets

    build = _build_module()
    g = tmp_path / "beanjar-guest/usr/share/beanjar/guest"
    build.stage_boards(g)
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib/installation.toml").write_text('version="test"\n')
    tree = paths.Tree(tmp_path / "lib")
    found = {}
    for target in targets.all_targets():
        broadcasts = [b for b, _ in targets.soc(target).broadcast_boards]
        for broadcast in [None] + broadcasts:
            rel = targets.board_cmdline_src(target, broadcast)
            installed = tree.src(rel)
            assert installed.startswith("/usr/share/beanjar/guest/boards/"), (target, rel)
            assert found.setdefault(installed, rel) == rel, f"{rel} and {found[installed]} collide"
            staged = g / os.path.relpath(installed, "/usr/share/beanjar/guest")
            assert staged.read_bytes() == (TOP / rel).read_bytes(), (target, broadcast)
    assert set(map(str, (g / "boards").rglob("*.cmdline"))) == {
        str(g / os.path.relpath(p, "/usr/share/beanjar/guest")) for p in found
    }
    for t in (("lm14", "netcast45"), ("m2", "simplesmart")):
        assert tree.src(targets.board_cmdline_src(t)) in found


def test_soc_board_files_keep_the_released_paths(tmp_path):
    """The host-guest contract both ways: every chip's board file stays at
    /usr/share/beanjar/guest/boards/<chip>/<name>, where a released host reads and a released guest
    package ships it; only the boards of other board sources are added beside them."""
    build = _build_module()
    g = tmp_path / "beanjar-guest/usr/share/beanjar/guest"
    build.stage_boards(g)
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib/installation.toml").write_text('version="test"\n')
    tree = paths.Tree(tmp_path / "lib")
    boards = sorted((TOP / "guest/layers/soc").glob("*/rootfs/etc/emulator/board*.cmdline"))
    assert boards
    for board in boards:
        chip, name = board.parts[-5], board.name
        released = f"/usr/share/beanjar/guest/boards/{chip}/{name}"
        assert tree.src(str(board.relative_to(TOP))) == released
        assert (g / "boards" / chip / name).read_bytes() == board.read_bytes()
    chips = {b.parts[-5] for b in boards}
    assert {p.name for p in (g / "boards").iterdir()} == chips | {"guest"}


def test_probe_current_by_source_hash_not_mtime(tmp_path):
    """A checkout writes new times on lls-probe.c; the probe's .sha256 keeps a shipped probe current,
    and a probe not made from the source fails without the SDK, naming the fix."""
    src = tmp_path / "guest/helpers/lls/lls-probe.c"
    src.parent.mkdir(parents=True)
    src.write_text("int main(void) { return 0; }\n")
    tree = paths.Tree(tmp_path)
    gcc = tmp_path / "gcc"
    gcc.write_text('#!/bin/sh\nwhile [ "$1" != -o ]; do shift; done\necho probe > "$2"\n')
    gcc.chmod(0o755)
    with mock.patch.object(lls, "SDK_GCC", str(gcc)):
        exe = lls.probe(tree)
    stamp = Path(exe + ".sha256")
    assert stamp.read_text().strip() == lls._probe_stamp(str(src), False)
    os.utime(src, (2e9, 2e9))  # newer than the probe, as a fresh checkout makes it
    nosdk = str(tmp_path / "missing-gcc")
    with mock.patch.object(lls, "SDK_GCC", nosdk):
        assert lls.probe(tree) == exe
        src.write_text("int main(void) { return 1; }\n")
        with pytest.raises(lls.Error, match="was not made from it.*sync-pi.sh"):
            lls.probe(tree)
        stamp.unlink()  # a probe from before the stamp: its time decides
        os.utime(exe, (3e9, 3e9))
        assert lls.probe(tree) == exe
    with mock.patch.object(lls, "SDK_GCC", str(gcc)):
        os.utime(exe, (1e9, 1e9))
        lls.probe(tree)
    assert stamp.read_text().strip() == lls._probe_stamp(str(src), False)
