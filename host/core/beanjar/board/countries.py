# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The countries a VM's board can be set to (vm.toml firmware.country, COUNTRY), from the
firmware's own tables (docs/user/board-profiles.md, "Choose the TV's country"; the record per
release line in docs/research/country-setting.md).

configd's RMM tables say which countries a board serves. etc/configd/data/rmmCountryGroup.json
names the RMM groups in two blocks, groupDVB and groupATSC (the latter also holds the ISDB lines,
JP and BR, so it reads "not DVB"), each group with its countries (codeName, alpha-3; shortName,
alpha-2). etc/configd/data/rmm/<group>/<country>/tv.rmm.json is one country's record, and from
webOS 5 (jhericurl) on it has the group's grpCode, the number a board's countryGrp= word gives
(US 2, EU 4, JP 18, ...). So the board's group is the RMM group whose grpCode is its countryGrp=,
and its countries are the ones a VM of that board may take: devicereset (webOS 5 on) keeps the
settings country group at the board's, and copies a settings country into lowlevelstorage's
factory countryName, from which it restores settings on every boot. A country of another group is
refused, and so is one of the other broadcast block, naming the board and the group it is in.

Releases before webOS 5 have no grpCode in those records (W16R to W19P) or no RMM tables at all
(WT1H, W15M; tvservice's own tables there): their boards' groups are not in the firmware's data,
and a country there is refused.

Read in place (lls.Root: unsquashfs -cat of the paks as the VM stacks them), a few small files.
"""

import json
import re

from . import lls
from .. import paths
from ..firmware import targets

RMM_GROUPS = "/etc/configd/data/rmmCountryGroup.json"
RMM_RECORD = "/etc/configd/data/rmm/{group}/{country}/tv.rmm.json"
# rmmCountryGroup.json's blocks and the broadcast standards (targets.BROADCASTS) of their boards
BLOCKS = {"groupDVB": ("dvb",), "groupATSC": ("atsc", "arib")}
CODE = re.compile(r"[A-Z]{3}")


class Error(Exception):
    pass


class Board:
    """The countries of a VM's board: group (the RMM group's name), grp_code (countryGrp=),
    block (BLOCKS' key), countries (its alpha-3 codes, sorted), where ({alpha-3: [(group, block)]}
    of every country the firmware's table has) and names ({alpha-3: alpha-2}, its shortName)."""

    def __init__(self, group, grp_code, block, countries, where, names=None):
        self.group = group
        self.grp_code = grp_code
        self.block = block
        self.countries = countries
        self.where = where
        self.names = names or {}

    def as_dict(self):
        return {
            "group": self.group,
            "country_group": self.grp_code,
            "broadcast_block": self.block,
            "countries": list(self.countries),
        }


def _json(root, path):
    blob = root.read(path)
    if blob is None:
        return None
    try:
        return json.loads(blob.decode("utf-8", errors="replace"))
    except ValueError as e:
        raise Error(f"the firmware's {path}: {e}") from None


def board(fw, target, tree=paths.TREE, broadcast=None, model=None):
    """The Board of the firmware directory fw's target, on the board of broadcast standard
    broadcast (targets.board_broadcast's), or of the retail model model (targets.board_model). Error where the firmware's data does not say."""
    if targets.platform(target) != "webOS":
        raise Error(f"{targets.platform(target)} has no country setting (webOS only)")
    if targets.release(target, fw).init_system != "systemd":
        raise Error(
            f"{targets.name(target)}: the country setting needs webOS 5 or later (the firmware's "
            "RMM records give the board's country group from then on)"
        )
    try:
        words = dict(
            w.split("=", 1)
            for w in lls.board_cmdline(target, tree, broadcast, model=model).split()
            if "=" in w
        )
    except lls.Error as e:
        raise Error(str(e)) from None
    if not words.get("countryGrp", "").isdigit():
        raise Error(f"{targets.name(target)}: the board's command line has no countryGrp=N")
    grp = int(words["countryGrp"])
    root = lls.Root(fw, target, tree)
    table = _json(root, RMM_GROUPS)
    if not isinstance(table, dict):
        raise Error(f"the firmware has no {RMM_GROUPS}: no country table")
    where = {}
    names = {}
    groups = []  # (block, group, [alpha-3])
    for block, gs in table.items():
        if block not in BLOCKS or not isinstance(gs, dict):
            continue
        for group, entry in gs.items():
            codes = sorted(
                {
                    c.get("codeName", "")
                    for c in (entry or {}).get("countryList", ())
                    if isinstance(c, dict) and CODE.fullmatch(c.get("codeName", ""))
                }
            )
            groups.append((block, group, codes))
            for c in (entry or {}).get("countryList", ()):
                if isinstance(c, dict) and c.get("codeName") in codes and c.get("shortName"):
                    names.setdefault(c["codeName"], c["shortName"])
            for c in codes:
                where.setdefault(c, []).append((group, block))
    own = [b for b in BLOCKS if broadcast in BLOCKS[b]] if broadcast else list(BLOCKS)
    # the board's block first: its group is read there before any other
    groups.sort(key=lambda g: g[0] not in own)
    for block, group, codes in groups:
        if not codes:
            continue
        record = _json(root, RMM_RECORD.format(group=group, country=codes[0]))
        if not isinstance(record, dict):
            continue
        if "grpCode" not in record:
            raise Error(
                f"the firmware's RMM records have no grpCode ({RMM_RECORD.format(group=group, country=codes[0])}): "
                "the board's country group is not in its data"
            )
        if record["grpCode"] == grp:
            if broadcast and block not in own:
                raise Error(
                    f"the board's country group {group} (countryGrp={grp}) is in the firmware's "
                    f"{block} block, not {broadcast}'s"
                )
            return Board(group, grp, block, codes, where, names)
    raise Error(f"no RMM group of the firmware has the board's countryGrp={grp}")


def check(fw, target, country, tree=paths.TREE, broadcast=None, model=None):
    """country (alpha-3, upper case) against the board's countries (board): the Board, or Error
    naming the board's countries and, for a country of another group, that group and block."""
    b = board(fw, target, tree, broadcast, model)
    if country in b.countries:
        return b
    std = broadcast or "/".join(BLOCKS[b.block])
    msg = (
        f"country {country!r} is not one of this board's: {targets.name(target)} boots its "
        f"{std} board, RMM group {b.group} (countryGrp={b.grp_code}): {', '.join(b.countries)}"
    )
    other = b.where.get(country)
    if other:
        msg += "; {} is in the firmware's {}".format(
            country, ", ".join(f"group {g} ({'/'.join(BLOCKS[blk])})" for g, blk in other)
        )
        others = sorted({BLOCKS[blk][0] for _, blk in other if blk != b.block})
        if others:
            msg += (
                f", another broadcast standard's: a {others[0]} board first "
                f"(broadcast={others[0]}, where the chip has one)"
            )
    else:
        msg += f"; the firmware's tables have no country {country!r}"
    raise Error(msg)


def parse(value):
    """A country setting's value: three letters, upper-cased; Error otherwise."""
    v = str(value).strip().upper()
    if not CODE.fullmatch(v):
        raise Error(f"country {value!r}: an ISO 3166 alpha-3 code such as USA or GBR")
    return v
