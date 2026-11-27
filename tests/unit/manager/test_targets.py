#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The firmware target table, guest/targets/ (targets.load): every file loads and passes its schema,
and a malformed file is refused with an error that names the file and the key. No VM, no firmware."""

import os
import shutil
import tomllib
from pathlib import Path

import pytest

from beanjar.firmware import targets

TOP = Path(__file__).resolve().parents[3]
TARGETS = os.path.join(TOP, "guest", "targets")


def test_every_file_loads():
    """Every TOML file of the tree parses, and the whole table passes the loader's checks:
    each target file is a target, each soc and release file a row, each partmap one a release
    names."""
    files = [os.path.join(d, n) for d, _, ns in os.walk(TARGETS) for n in ns if n.endswith(".toml")]
    assert files
    for f in files:
        with open(f, "rb") as fh:
            tomllib.load(fh)
    table = targets.load(TARGETS)
    names = sorted(n[:-5] for n in os.listdir(TARGETS) if n.endswith(".toml"))
    assert sorted(targets.name(p) for p in table["TARGETS"]) == names
    assert sorted(table["SOCS"]) == sorted(n[:-5] for n in os.listdir(os.path.join(TARGETS, "soc")))
    assert sorted(table["RELEASES"]) == sorted(
        n[:-5] for n in os.listdir(os.path.join(TARGETS, "release"))
    )
    for target in table["TARGETS"]:
        soc = table["TARGET_SOCS"].get(target) or table["SOCS"][target[0]]
        rel = table["TARGET_RELEASES"].get(target) or table["RELEASES"][target[1]]
        assert isinstance(soc, targets.Soc)
        assert isinstance(rel, targets.Release)
        assert not isinstance(rel.partmap, str), targets.name(target)
    assert table["WEBOS_ORDER"][0] == "afro"
    assert table["MODELS"] == targets.MODELS


def test_base_replaces_keys_whole():
    """A row with base is base's with its keys replaced: k8lp's broadcast_params empty, the rest
    of k7lp's kept."""
    k7, k8 = targets.SOCS["k7lp"], targets.SOCS["k8lp"]
    assert k7.broadcast_params
    assert k8.broadcast_params == ()
    assert k8.light_bus == k7.light_bus
    assert targets.RELEASES["beehive"].paks == targets.RELEASES["afro"].paks


def test_firmware_params_by_model_code():
    """Soc.firmware_params apply to the firmware of their model code only: W21Z, the projector on
    W21P's K7LP, gets its DLP controller on I2C; W21P, on the same target, nothing."""
    k7 = ("k7lp", "kisscurl")
    assert targets.firmware_module_params(k7, "03.00.27.01-HE_PJT_W21Z_AAAAGLAA") == {
        "lgemu": ("i2c_dev=dlpc:3:0x1a",)
    }
    assert targets.firmware_module_params(k7, "03.70.25.01-HE_DTV_W21P_AFADATAA") == {}
    assert targets.firmware_module_params(k7, None) == {}


def test_card_only_on_arib_boards():
    """The stub card (webos_tv's acas=1) is on O22's Japanese board only, not on its US or global
    one; its ID is 12 hex digits from the serial, the same for the same serial, starting 0xBE."""
    o22 = ("o22", "queue")
    assert targets.card_module(o22, "arib") == "webos_tv"
    assert targets.card_module(o22, "atsc") is None
    assert targets.card_module(o22, "dvb") is None
    assert targets.card_module(o22) is None
    a, b = targets.card_id("292DADA78604"), targets.card_id("501RAXX00001")
    assert a == targets.card_id("292DADA78604") and a != b
    assert len(a) == 12 and a.startswith("be") and int(a, 16)


def test_tee_device_keyed_on_the_client(monkeypatch):
    """lgemu.ko's bare /dev/tee0 (tee=1) where the firmware's rootfs has OP-TEE's supplicant and no
    chip module has its TAs; none on a chip whose module has them (rtk.ko's tee=1)."""
    asked = []

    def has(fw, path):
        asked.append((fw, path))
        return fw == "with"

    monkeypatch.setattr(targets.paths, "rootfs_has", has)
    lm = ("lm21ann", "ponytail")
    assert targets.tee_module(lm) is None
    assert targets.tee_device(lm, "with") == ("tee=1",)
    assert targets.tee_device(lm, "without") == ()
    assert asked == [("with", targets.TEE_CLIENT), ("without", targets.TEE_CLIENT)]
    k24 = ("k24", "ponytail")
    assert targets.tee_module(k24) == "rtk"
    assert targets.tee_device(k24, "with") == ()

    def unreadable(fw, path):
        raise ValueError("no rootfs.pak")

    monkeypatch.setattr(targets.paths, "rootfs_has", unreadable)
    with pytest.raises(targets.Error, match="OP-TEE's client"):
        targets.tee_device(lm, "x")


class Malformed:
    def __init__(self, tmp):
        self.dir = os.path.join(tmp, "targets")
        shutil.copytree(TARGETS, self.dir)

    def edit(self, rel, old, new):
        path = os.path.join(self.dir, rel)
        with open(path) as f:
            text = f.read()
        assert old in text
        with open(path, "w") as f:
            f.write(text.replace(old, new, 1))
        return path

    def refused(self, *words):
        with pytest.raises(targets.Error) as e:
            targets.load(self.dir)
        for w in words:
            assert w in str(e.value)
        return str(e.value)


@pytest.fixture
def malformed(tmp_path):
    return Malformed(str(tmp_path))


def test_unknown_key(malformed):
    path = malformed.edit(
        "soc/k8hp.toml", 'base = "k8lp"\n', 'base = "k8lp"\nboard_cmdlin = "/x"\n'
    )
    malformed.refused(path, "unknown key 'board_cmdlin'")


def test_wrong_type(malformed):
    path = malformed.edit("release/afro.toml", "cap_last_cap = 36", 'cap_last_cap = "36"')
    malformed.refused(path, "cap_last_cap", "expected an integer")


def test_wrong_type_nested(malformed):
    path = malformed.edit("release/afro.toml", "tool_option_words = 7", "tool_option_words = true")
    malformed.refused(path, "nvm.tool_option_words", "expected an integer")


def test_missing_required_key(malformed):
    path = malformed.edit("release/afro.toml", 'init_system = "upstart"\n', "")
    malformed.refused(path, "missing key 'init_system'")


def test_value_not_allowed(malformed):
    path = malformed.edit("release/afro.toml", 'init_system = "upstart"', 'init_system = "runit"')
    malformed.refused(path, "init_system", "expected one of")


def test_syntax_error(malformed):
    path = malformed.edit("h13-afro.toml", "models = [", "models = [[")
    malformed.refused(path)


def test_unknown_base(malformed):
    path = malformed.edit("soc/k8hp.toml", 'base = "k8lp"', 'base = "k9lp"')
    malformed.refused(path, "base", "k9lp")


def test_unknown_partmap(malformed):
    path = malformed.edit("release/mullet.toml", 'partmap = "mullet"', 'partmap = "mulet"')
    malformed.refused(path, "partmap", "mulet")


def test_target_without_soc(malformed):
    os.rename(
        os.path.join(malformed.dir, "h13-afro.toml"), os.path.join(malformed.dir, "h14-afro.toml")
    )
    malformed.refused("h14-afro.toml", "soc/h14.toml")


def test_bring_up_disagrees(malformed):
    path = malformed.edit("k8hp-queue.toml", 'bring_up = ["W22H"]\n', "")
    malformed.refused(path, "W22H", "bring_up")


def test_target_soc_row(malformed):
    path = malformed.edit("m2-simplesmart.toml", "board_src = ", "board_srcs = ")
    malformed.refused(path, "soc", "unknown key 'board_srcs'")


def test_tested_by_default():
    """A target file without tested is tested: no warning for it."""
    t = targets.TARGETS[("h13", "afro")]
    assert t.tested
    assert targets.tested(("h13", "afro"))
    assert targets.untested_warning(("h13", "afro")) is None


def test_untested_target(malformed):
    """tested = false makes that target untested and no other."""
    malformed.edit(
        "k8hpp-number1.toml", 'models = ["W23H"]\n', 'models = ["W23H"]\ntested = false\n'
    )
    table = targets.load(malformed.dir)
    assert not table["TARGETS"][("k8hpp", "number1")].tested
    for p, t in table["TARGETS"].items():
        if p != ("k8hpp", "number1"):
            assert t.tested == targets.TARGETS[p].tested, targets.name(p)


def test_untested_warning():
    untested = targets.TARGETS[("k8hpp", "number1")]._replace(tested=False)
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(targets.TARGETS, ("k8hpp", "number1"), untested)
        assert "k8hpp-number1" in targets.untested_targets()
        assert "h13-afro" not in targets.untested_targets()
        assert (
            targets.untested_warning(("k8hpp", "number1"))
            == "k8hpp-number1: not tested yet: may not boot"
        )


def test_tested_true_refused(malformed):
    path = malformed.edit("h13-afro.toml", "models = [", "tested = true\nmodels = [")
    malformed.refused(path, "tested", "only tested = false")


def test_tested_wrong_type(malformed):
    path = malformed.edit("h13-afro.toml", "models = [", 'tested = "no"\nmodels = [')
    malformed.refused(path, "tested", "expected true or false")


INSTALLED_TARGETS = "/usr/share/beanjar/guest/targets"


def without_definitions(tmp_path, installed):
    """A copy of the host core with no board definitions: a checkout without guest/targets, or an
    installed beanjar (installation.toml beside core/) without beanjar-guest. Its PYTHONPATH."""
    core = tmp_path / ("core" if installed else "host/core")
    shutil.copytree(
        TOP / "host/core/beanjar", core / "beanjar", ignore=shutil.ignore_patterns("__pycache__")
    )
    if installed:
        (tmp_path / "installation.toml").write_text('version = "0.0.0"\n')
    return str(core)


@pytest.mark.parametrize("installed", [False, True])
def test_missing_definitions(tmp_path, installed):
    """Without board definitions the core still imports (beanjar --help runs), and the first use of
    the table is an Error, a base.Error the CLI prints, saying what is missing: beanjar-guest for an
    installed beanjar."""
    import subprocess
    import sys

    if installed and os.path.exists(INSTALLED_TARGETS):
        pytest.skip(f"{INSTALLED_TARGETS} is installed here")
    env = dict(os.environ, PYTHONPATH=without_definitions(tmp_path, installed))
    env.pop("PYTHONDONTWRITEBYTECODE", None)

    def run(*args):
        return subprocess.run(
            [sys.executable, *args], env=env, cwd=tmp_path, capture_output=True, text=True
        )

    assert run("-m", "beanjar", "--help").returncode == 0
    r = run(
        "-c",
        "from beanjar import base; from beanjar.firmware.install import targets\n"
        "try:\n"
        "    targets.of_firmware('W22O', 'queue')\n"
        "except targets.Error as e:\n"
        "    assert isinstance(e, base.Error)\n"
        "    print(e)\n",
    )
    assert r.returncode == 0, r.stderr
    where = INSTALLED_TARGETS if installed else str(tmp_path / "guest/targets")
    what = (
        "beanjar needs beanjar-guest of its host-guest contract installed"
        if installed
        else "the checkout has no guest/targets"
    )
    assert r.stdout == f"no board definitions at {where}: {what}\n"
    r = run("-m", "beanjar.launch", "config", "TARGETS")
    assert (r.returncode, r.stdout) == (1, "")
    assert f"no board definitions at {where}: {what}" in r.stderr


def test_empty_definitions_directory(tmp_path):
    """A definitions directory with no target files in it is as missing as no directory."""
    import subprocess
    import sys

    env = dict(os.environ, PYTHONPATH=without_definitions(tmp_path, False))
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    (tmp_path / "guest/targets").mkdir(parents=True)
    r = subprocess.run(
        [
            sys.executable,
            "-c",
            "from beanjar.firmware.install import targets\ntargets.of_firmware('W22O', 'queue')",
        ],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert r.returncode != 0
    assert (
        f"no board definitions at {tmp_path / 'guest/targets'}: the checkout has no guest/targets"
        in r.stderr
    )
