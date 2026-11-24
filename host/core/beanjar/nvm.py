# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The TV's NVM EEPROM as a factory leaves it: the launcher's part of the I2C EEPROM that lgemu.ko serves to the firmware
(guest/kernel/modules/lgemu/i2c.c) from the state disk targets.Release.nvm.eeprom.

A blank image (all zero: the launcher's new sparse file) is a new chip: 0xFF, with the firmware's
own default records programmed as a factory would, from the board parameters the bootloader copies
from it onto the command line (guest/layers/soc/<chip>/rootfs/etc/emulator/board.cmdline). The layouts
are the release's (targets.Release.nvm, Nvm):
  - the ModelInfoDB (tvservice's .data object gModelInfoDB; on every release here 44 bytes: mark
    0x20130002, model name[13] at +4, serial[17] at +17, ...) with modelName= and serialNum=;
  - the ToolOptionDB (gToolOptionDB, 52 bytes; 76 on webOS 3) with the words of ToolOpt=
    (Nvm.tool_option_*: seven 16-bit words from +4 on webOS 1, eight on webOS 2, nine 32-bit
    words from +8 on webOS 3);
  - on webOS 3 the ContinentOptionDB (gContinentOptDB), which tvconfig-gen aborts without, set to
    the model's area (targets.Soc.continent) from the firmware's continent table;
  - on webOS 3 the Last_Input_info DB (gLastInputInfoDB, the TV input), which tvservice writes
    only in factory mode or before FirstUse has run (Nvm.lastinput_offset);
  - on webOS 4 the ToolOptionDB's input-port bytes (Nvm.inputs_field), the model's (Soc.inputs):
    the user input map, which tvservice reads only with ToolOpt2's input index 0 (otherwise it
    takes the firmware's row of that index);
  - the FactoryDB (gFactoryDB, Nvm.factory_offset) as the factory leaves it: substrate mode off,
    which tvservice does not write to a blank chip itself; without it the set stays in factory
    state and Quick Start+ never suspends.
Every other DB is left blank, so the first boot of a state writes tvservice's defaults ("First
BootUp Detect"). The records are read from the ELF file Nvm.records names (its dynamic symbol
table and the loadable segment that holds each object), where the libhal_lg115x shim read them
from the running process before (dlsym, dladdr1). On later starts the tool options follow
ToolOpt= (sync_tool_options) and the model name modelName= (sync_model), as lxboot's copy and the
NVM do on a TV, and the serial follows the VM's own (sync_serial: vm.toml's serial, which
guest/initramfs/rootfs/init also puts in serialNum=). The area is a factory's only: a programmed
ContinentOptionDB is the TV's (its menus change it) and is not synced. The debug status follows
the VM's debug_mode at every start: a plain byte (sync_debug_status), and on webOS 4.x the
32-byte block that the SoC encrypts on a TV and the emulator keeps clear (sync_debug_block,
beanjar/nvmdebug.py).
"""

import collections
import contextlib
import os
import re
import struct
import subprocess

from . import targets, paths

SIZE = 0x8000
# webOS 3's ContinentOptionDB: 15 16-bit option words from +8 (continent_record)
CONTINENT_WORDS = 15


class Error(Exception):
    pass


# ---------------------------------------------------------------------------- the firmware's ELF
def elf_objects(data, names):
    """{name: bytes} of the defined data objects names in the ELF32 little-endian file data (an
    executable), by its dynamic symbol table (what dlsym finds) and its loadable segments; a name
    it does not define is left out. An object that a dynamic relocation writes into is refused:
    its bytes in the file are not what the process has."""
    if data[:4] != b"\x7fELF" or data[4] != 1 or data[5] != 1:
        raise Error("not a 32-bit little-endian ELF file")
    e_phoff, e_shoff = struct.unpack_from("<II", data, 28)
    e_phentsize, e_phnum, e_shentsize, e_shnum = struct.unpack_from("<HHHH", data, 42)
    loads, dynamic = [], None
    for i in range(e_phnum):
        p_type, p_offset, p_vaddr, _, p_filesz, p_memsz = struct.unpack_from(
            "<IIIIII", data, e_phoff + i * e_phentsize
        )
        if p_type == 1:
            loads.append((p_vaddr, p_offset, p_filesz, p_memsz))
        elif p_type == 2:
            dynamic = (p_offset, p_filesz)
    sections = [
        struct.unpack_from("<IIIIIIIIII", data, e_shoff + i * e_shentsize) for i in range(e_shnum)
    ]
    dynsym = [s for s in sections if s[1] == 11]  # SHT_DYNSYM
    if not dynsym:
        raise Error("no dynamic symbol table")
    found = {}
    # the dynamic symbols first, then the full table (SHT_SYMTAB) where the file keeps one, for
    # file-local objects (tvservice's _aDefineContinent)
    for table in dynsym[:1] + [s for s in sections if s[1] == 2][:1]:
        _, _, _, _, sym_off, sym_size, strtab_index, _, _, sym_entsize = table
        str_off = sections[strtab_index][4]
        for off in range(sym_off, sym_off + sym_size, sym_entsize):
            st_name, st_value, st_size, st_info, _, st_shndx = struct.unpack_from(
                "<IIIBBH", data, off
            )
            if st_info & 0xF != 1 or st_shndx == 0:  # a defined STT_OBJECT
                continue
            end = data.index(b"\0", str_off + st_name)
            name = data[str_off + st_name : end].decode()
            if name in names and name not in found:
                found[name] = (st_value, st_size)
    relocated = _relocated(data, dynamic, loads)
    out = {}
    for name, (addr, size) in found.items():
        if any(addr <= r < addr + size for r in relocated):
            raise Error(f"{name} is written by a dynamic relocation")
        for vaddr, offset, filesz, memsz in loads:
            if vaddr <= addr and addr + size <= vaddr + memsz:
                blob = data[
                    offset + addr - vaddr : offset + min(addr + size, vaddr + filesz) - vaddr
                ]
                out[name] = blob.ljust(size, b"\0")  # the part past filesz is .bss
                break
        else:
            raise Error(f"{name} is in no loadable segment")
    return out


def _relocated(data, dynamic, loads):
    """The addresses the dynamic relocations (DT_REL, DT_JMPREL; ARM uses REL) write."""
    if not dynamic:
        return set()
    tags = {}
    for off in range(dynamic[0], dynamic[0] + dynamic[1], 8):
        tag, val = struct.unpack_from("<iI", data, off)
        if tag == 0:
            break
        tags[tag] = val

    def file_offset(addr):
        for vaddr, offset, filesz, _ in loads:
            if vaddr <= addr < vaddr + filesz:
                return offset + addr - vaddr
        raise Error(f"relocations at 0x{addr:x} are in no segment")

    out = set()
    for table, size in ((17, 18), (23, 2)):  # DT_REL/DT_RELSZ, DT_JMPREL/DT_PLTRELSZ
        if table in tags and size in tags:
            start = file_offset(tags[table])
            for off in range(start, start + tags[size], 8):
                out.add(struct.unpack_from("<I", data, off)[0])
    return out


def records_file(fw, nvm):
    """The bytes of the firmware directory fw's Nvm.records file (tvservice), read with unsquashfs."""
    pak, path = nvm.records
    r = subprocess.run(
        ["unsquashfs", "-cat", "-no-wildcards", os.path.join(fw, pak + ".pak"), path.lstrip("/")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if r.returncode:
        raise Error(f"{pak}.pak:{path}: {r.stderr.decode(errors='replace').strip() or 'not read'}")
    return r.stdout


def default_records(fw, nvm, tree=paths.TREE, data=None):
    """{name: bytes}: the default records of the firmware directory fw's Nvm.records file (data:
    its bytes where they are already read, else read with unsquashfs)."""
    pak, path = nvm.records
    if data is None:
        data = records_file(fw, nvm)
    try:
        return elf_objects(
            data,
            ("gModelInfoDB", "gToolOptionDB", "gContinentOptDB", "gLastInputInfoDB", "gFactoryDB")
            + ((nvm.continent_table[0],) if nvm.continent_table else ()),
        )
    except (Error, struct.error, ValueError) as e:
        raise Error(f"{pak}.pak:{path}: {e}") from None


# ---------------------------------------------------------------------------- the board
# The TV's debug status by the name vm.toml's debug_mode has: the number of the board's debugMode=
# word (and of the EEPROM's debug status, U-Boot's DDI_NVM_SetDebugStatus keeps the two in agreement).
DEBUG_MODES = {"debug": 3, "event": 4, "release": 5}


def debug_mode_number(name):
    """The debugMode= number of the setting debug_mode's value name; None for no setting (the board's own)."""
    if not name:
        return None
    if name not in DEBUG_MODES:
        raise Error(f"debug_mode={name}: one of {', '.join(DEBUG_MODES)}")
    return DEBUG_MODES[name]


def with_debug_mode(words, number):
    """The board's words with its debugMode=N word's N replaced by number (None: as they are). The
    board must have the word: one debugMode= stays on the command line."""
    if number is None:
        return list(words)
    if not any(w.startswith("debugMode=") for w in words):
        raise Error("the board's command line has no debugMode=N")
    return [f"debugMode={number}" if w.startswith("debugMode=") else w for w in words]


def board_params(target, tree=paths.TREE, broadcast=None):
    """{name: value} of the target's board.cmdline (for the broadcast standard broadcast:
    targets.board_cmdline), as guest/initramfs/rootfs/init puts it on /proc/cmdline
    (comment lines left out, the first NAME= of each; the debugMode of the VM's setting is
    board_words'.)"""
    path = tree.src(targets.board_cmdline_src(target, broadcast))
    out = {}
    with open(path) as f:
        for text in f:
            if text.startswith("#"):
                continue
            for word in text.split():
                name, eq, value = word.partition("=")
                if eq and name not in out:
                    out[name] = value
    return out


def board_words(target, tree=paths.TREE, broadcast=None, debug_mode=None):
    """The words of the target's board.cmdline in its order (comment lines left out): what
    guest/initramfs/rootfs/init appends to /proc/cmdline, or the launcher puts first on the kernel's
    command line (Release.board_in_kernel). debug_mode: the number the VM's debug_mode setting
    gives its debugMode= word (DEBUG_MODES), else the board's own."""
    with open(tree.src(targets.board_cmdline_src(target, broadcast))) as f:
        words = [w for text in f if not text.startswith("#") for w in text.split()]
    return with_debug_mode(words, debug_mode)


def board_graphic(target, tree=paths.TREE, broadcast=None):
    """ "WxH" of the target's model's graphic (OSD) plane: its board.cmdline's hwopt= (targets.graphic_resolution)."""
    return "%dx%d" % targets.screen_resolution(
        target, board_params(target, tree, broadcast).get("hwopt")
    )


def board_ram(target, tree=paths.TREE, broadcast=None):
    """The RAM in MiB of the target's model: its board.cmdline's hwopt= DDR size (targets.ram)."""
    return targets.ram(target, board_params(target, tree, broadcast).get("hwopt"))


def tool_options(record, nvm, text):
    """ToolOpt=o1:...:oN (text, the bootloader's copy of the ToolOptionDB's words) into the
    ToolOptionDB bytearray record, as little-endian words; how many words were given."""
    n = 0
    for word in text.split(":") if text else ():
        if n == nvm.tool_option_words or not re.fullmatch(r"\d+", word):
            break
        at = nvm.tool_option_first + nvm.tool_option_word_bytes * n
        record[at : at + nvm.tool_option_word_bytes] = (
            int(word) & (1 << 8 * nvm.tool_option_word_bytes) - 1
        ).to_bytes(nvm.tool_option_word_bytes, "little")
        n += 1
    return n


def given_words(nvm):
    """How many tool option words the bootloader puts in ToolOpt= (Nvm.tool_option_given): every
    word but on Realtek's U-Boot (6 of 9)."""
    return nvm.tool_option_given or nvm.tool_option_words


def _set_field(record, field, text):
    """A ModelInfoDB field (offset, length) of record: text, at most length - 1 bytes, zero-padded."""
    at, length = field
    record[at : at + length] = text.encode()[: length - 1].ljust(length, b"\0")


def _get_field(record, field):
    at, length = field
    return bytes(record[at : at + length]).split(b"\0")[0].decode(errors="replace")


def continent_record(records, nvm, continent):
    """The ContinentOptionDB (bytes) a factory programs for the area continent (Soc.continent:
    the continent index, language-country group and HW setting group), from the default record
    (its valid mark) and the firmware's continent table (Nvm.continent_table): the record's
    AREA_OPTION2 at +4 (index bits 0-6, language-country 7-11, HW setting 12-15), modifiedSub 0 at
    +6, and from +8 the continent's 15 option words (stVideo..stReserved3), which follow the
    4-byte name pointer in the table's entry. Without continent: the default record."""
    default = records.get("gContinentOptDB")
    if not default or len(default) < 4:
        return None
    if not continent:
        return default
    symbol, stride = nvm.continent_table
    table = records.get(symbol)
    index, language, hw = continent
    if not table or len(table) < (index + 1) * stride:
        raise Error(f"no {symbol} entry {index}")
    record = bytearray(default)
    struct.pack_into("<HH", record, 4, index | language << 7 | hw << 12, 0)
    record[8 : 8 + CONTINENT_WORDS * 2] = table[
        index * stride + 4 : index * stride + 4 + CONTINENT_WORDS * 2
    ]
    return bytes(record)


def inputs(record, nvm, ports):
    """The model's input-port bytes (Soc.inputs) into the ToolOptionDB bytearray record, on a
    release that has them (Nvm.inputs_field); True if any changed."""
    if not nvm.inputs_field or ports is None:
        return False
    at, length = nvm.inputs_field
    if len(ports) != length:
        raise Error(f"{len(ports)} input-port bytes, the ToolOptionDB has {length}")
    if record[at : at + length] == ports:
        return False
    record[at : at + length] = ports
    return True


def program(records, nvm, board, serial, say, continent=None, ports=None):
    """A new chip's image (bytes): 0xFF with the records (default_records) and the board's
    values in them; continent: the model's area (Soc.continent), on a release with a
    ContinentOptionDB; ports: the model's input ports (Soc.inputs), on a release with
    Nvm.inputs_field."""
    image = bytearray(b"\xff" * SIZE)
    model_db, option_db = records.get("gModelInfoDB"), records.get("gToolOptionDB")
    option_size = nvm.tool_option_first + nvm.tool_option_word_bytes * nvm.tool_option_words
    model_size = max(sum(nvm.model_field), sum(nvm.serial_field))
    if not model_db or len(model_db) < model_size or not option_db or len(option_db) < option_size:
        raise Error("no default gModelInfoDB and gToolOptionDB records")
    model_db, option_db = bytearray(model_db[:0x100]), bytearray(option_db[:0x100])
    given = tool_options(option_db, nvm, board.get("ToolOpt", ""))
    if given != given_words(nvm):
        say(
            f"EEPROM: ToolOpt= has {given} of {given_words(nvm)} words: the rest are the firmware's defaults"
        )
    if board.get("modelName"):
        _set_field(model_db, nvm.model_field, board["modelName"])
    if serial:
        _set_field(model_db, nvm.serial_field, serial)
    inputs(option_db, nvm, ports)
    image[nvm.modelinfo_offset : nvm.modelinfo_offset + len(model_db)] = model_db
    image[nvm.tooloption_offset : nvm.tooloption_offset + len(option_db)] = option_db
    if nvm.continent_offset:
        record = continent_record(records, nvm, continent)
        if record:
            image[nvm.continent_offset : nvm.continent_offset + min(len(record), 0x100)] = record[
                :0x100
            ]
    if nvm.lastinput_offset:
        record = records.get("gLastInputInfoDB")
        if not record:
            raise Error("no default gLastInputInfoDB record")
        image[nvm.lastinput_offset : nvm.lastinput_offset + min(len(record), 0x100)] = record[
            :0x100
        ]
    if nvm.factory_offset:
        record = records.get("gFactoryDB")
        if not record:
            raise Error("no default gFactoryDB record")
        image[nvm.factory_offset : nvm.factory_offset + min(len(record), 0x200)] = record[:0x200]
    model, serial = _get_field(model_db, nvm.model_field), _get_field(model_db, nvm.serial_field)
    say(
        f"EEPROM blank: a new chip (0xFF), programmed with model {model!r}, serial {serial!r}, "
        f"tool options {board.get('ToolOpt') or '(defaults)'}"
        + (f", continent {continent[0]}" if nvm.continent_offset and continent else "")
    )
    return bytes(image)


def sync_tool_options(image, nvm, board, say, ports=None):
    """A programmed image (bytearray): its tool options follow ToolOpt=, when its ToolOptionDB is
    valid and every word the bootloader passes is given (given_words), and its input ports the
    model's (ports, Soc.inputs). True if it changed."""
    at, size = (
        nvm.tooloption_offset,
        nvm.tool_option_first + nvm.tool_option_word_bytes * nvm.tool_option_words,
    )
    if nvm.inputs_field:
        size = max(size, sum(nvm.inputs_field))
    if struct.unpack_from("<I", image, at)[0] != nvm.tool_option_mark:
        return False
    want = bytearray(image[at : at + size])
    if (
        tool_options(want, nvm, board.get("ToolOpt", "")) == given_words(nvm)
        and want != image[at : at + size]
    ):
        say(f"EEPROM tool options set from board.cmdline: {board['ToolOpt']}")
    else:
        want[:] = image[at : at + size]
    if inputs(want, nvm, ports):
        say("EEPROM input ports set from the model's")
    if want == image[at : at + size]:
        return False
    image[at : at + size] = want
    return True


def sync_model(image, nvm, model, say):
    """A programmed image (bytearray): its ModelInfoDB's model name follows modelName=, as lxboot's
    copy does on a TV. True if it changed."""
    at = nvm.modelinfo_offset
    if not model or struct.unpack_from("<I", image, at)[0] != nvm.modelinfo_mark:
        return False
    record = bytearray(image[at : at + sum(nvm.model_field)])
    _set_field(record, nvm.model_field, model)
    if record == image[at : at + len(record)]:
        return False
    image[at : at + len(record)] = record
    say(f"EEPROM model name set from board.cmdline: {model}")
    return True


def sync_serial(image, nvm, serial, say):
    """A programmed image (bytearray): its ModelInfoDB's serial follows the VM's. True if it
    changed."""
    at = nvm.modelinfo_offset
    if not serial or struct.unpack_from("<I", image, at)[0] != nvm.modelinfo_mark:
        return False
    record = bytearray(image[at : at + sum(nvm.serial_field)])
    _set_field(record, nvm.serial_field, serial)
    if record == image[at : at + len(record)]:
        return False
    image[at : at + len(record)] = record
    say(f"EEPROM serial set: {serial}")
    return True


def replace_serial(image, old, new, width=17):
    """image (bytes) with every record of the serial old, NUL-padded to width bytes (the ModelInfoDB's
    serial field, LLS's factory serialnumber item: 17 bytes on every release), replaced by new the same
    way: (the new image, how many). A clone's EEPROM, whose chip (LLS's, webOS 5 on) a start does not
    bring in line with the VM's serial."""
    if not old or not new or len(old) >= width or len(new) >= width:
        return bytes(image), 0
    before, after = old.encode().ljust(width, b"\0"), new.encode().ljust(width, b"\0")
    count = bytes(image).count(before)
    return bytes(image).replace(before, after), count


def sync_debug_status(image, nvm, mode, say=print):
    """The debug status byte of the bytearray image (Nvm.debug_status_offset) set to mode (the number,
    DEBUG_MODES: the VM's debug_mode, else the board's debugMode=), so that tvservice
    (OSA_MD_GetDebugStatus, the luna property debugLevel that faultmanager reads) always agrees with
    the kernel's word; a blank chip's 0xFF would read as RELEASE. Whether the image changed. A
    release without the plain byte (W18R) has nothing here; its block is sync_debug_block's."""
    at = nvm.debug_status_offset
    value = nvm.debug_status_values[mode - DEBUG_MODES["debug"]]
    if at is None or image[at] == value:
        return False
    image[at] = value
    say(f"EEPROM debug status set: {value}")
    return True


def sync_debug_block(image, nvm, mode, say=print, rand=os.urandom):
    """The debug status block of the bytearray image (Nvm.debug_block_offset, webOS 4.x) set to
    the clear block of mode (DEBUG_MODES' number, which is tvservice's 3/4/5 too) in the firmware's
    encoding (beanjar/nvmdebug.py), which the soc layer's identity cipher hands tvservice as it is.
    A block that already reads as mode is left alone (its random bytes are not made again at every
    start). Whether the image changed. A release without the block: nothing."""
    from . import nvmdebug

    at = nvm.debug_block_offset
    if at is None:
        return False
    if nvmdebug.decode(image[at : at + nvmdebug.SIZE]) == mode:
        return False
    image[at : at + nvmdebug.SIZE] = nvmdebug.encode(mode, rand)
    say(f"EEPROM debug status block set: {mode}")
    return True


def check_debug_block(data, nvm):
    """Error unless the tvservice of bytes data reads its debug status where the release says: the
    block at Nvm.debug_block_offset and, where it reads one, the plain copy at debug_status_offset
    (beanjar/nvmdebug.py decodes its DIL_NVM_Load_DebugStatus)."""
    from . import nvmdebug

    try:
        f = nvmdebug.fields(data)
    except nvmdebug.Error as e:
        raise Error(f"the debug status block: {e}") from None
    if (f["offset"], f["plain"]) != (nvm.debug_block_offset, nvm.debug_status_offset):

        def where(block, plain):
            return f"0x{block:x}" + (f" and a plain copy at 0x{plain:x}" if plain else "")

        raise Error(
            f"the debug status block: its tvservice reads {where(f['offset'], f['plain'])},"
            f" not the release's {where(nvm.debug_block_offset, nvm.debug_status_offset)}"
        )


# A VM's new chip, worked out ahead (prepare.py, at create and when the firmware, the board or the
# VM's serial or debug mode change): image, the bytes a blank state's EEPROM gets (None: it stays
# blank, note says why); records, LLS's probe output for the board (records.txt, lls.cache_dir),
# whose debugstatus item a programmed chip takes when the VM's debug mode changes; panel_hz, the
# panel's refresh the firmware's own generator decodes from the board (lls.panel_rate; None where
# it was not run: before webOS 5).
Chip = collections.namedtuple("Chip", "image records note panel_hz", defaults=(None,))


def new_chip(
    fw,
    target,
    serial=None,
    tree=paths.TREE,
    say=print,
    fw_name=None,
    broadcast=None,
    debug_mode=None,
    country=None,
):
    """The Chip a blank EEPROM of the VM gets: the firmware's own default records programmed as a
        factory would (program; on a release whose chip lowlevelstorage owns, Nvm.lls, LLS's own
        records, beanjar/lls.py), with the board's values, serial (the VM's own, else board.cmdline's
        serialNum=) and debug status (debug_mode: DEBUG_MODES' number of the VM's setting, else the
        board's debugMode=); on an LLS chip the factory countryName country (the VM's, where it has
    one: lls.py). Reads the firmware; Error if LLS's records cannot be made. A firmware
        without tvservice's records gives no image, and note says why."""
    nvm = targets.release(target).nvm
    if nvm.lls:
        from . import lls

        try:
            cdir = lls.cache_dir(
                fw,
                fw_name or os.path.basename(os.path.normpath(fw)),
                target,
                tree,
                say,
                serial=serial,
                broadcast=broadcast,
                debug_mode=debug_mode,
                country=country,
            )
            with open(os.path.join(cdir, "eeprom.img"), "rb") as f:
                image = f.read()
            with open(os.path.join(cdir, "records.txt")) as f:
                records = f.read()
        except (lls.Error, OSError, subprocess.SubprocessError) as e:
            raise Error(f"EEPROM initialization failed: {e}") from e
        return Chip(image, records, None, lls.panel_rate(cdir))
    if nvm.records is None and nvm.factory:
        mode = (
            debug_mode
            if debug_mode is not None
            else int(board_params(target, tree, broadcast)["debugMode"])
        )
        return Chip(factory_chip(fw, nvm, mode), None, None)
    if nvm.records is None:
        return Chip(None, None, None)
    board = board_params(target, tree, broadcast)
    block = nvm.debug_block_offset is not None
    try:
        data = records_file(fw, nvm) if block else None
        records = default_records(fw, nvm, tree, data)
    except Error as e:
        return Chip(None, None, str(e))
    if block:
        check_debug_block(data, nvm)  # a build that reads it elsewhere is refused
    try:
        new = program(
            records,
            nvm,
            board,
            serial or board.get("serialNum", ""),
            say,
            targets.soc(target).continent,
            targets.soc(target).inputs,
        )
    except Error as e:
        return Chip(None, None, str(e))
    new = bytearray(new)
    mode = debug_mode if debug_mode is not None else int(board["debugMode"])
    sync_debug_status(new, nvm, mode, say)
    sync_debug_block(new, nvm, mode, say)
    return Chip(bytes(new), None, None)


def prepare(
    img, target, chip, serial=None, tree=paths.TREE, say=print, broadcast=None, debug_mode=None
):
    """Bring the EEPROM image img (a state disk, SIZE bytes) to what the VM's board says before it
    starts, from the VM's prepared Chip (new_chip) and the board; nothing of the firmware is read.
    A blank image becomes chip.image (none: it stays blank, said); a programmed one has its tool
    options, model name and serial synced (serial: the VM's own, SERIAL, else board.cmdline's
    serialNum=). A release whose chip lowlevelstorage owns (Nvm.lls, webOS 5 on) gets prepare_lls.
    debug_mode: the number of the VM's debug_mode setting (DEBUG_MODES), else the board's own
    debugMode=: a chip whose release keeps the status as a plain byte (Nvm.debug_status_offset)
    or a block (Nvm.debug_block_offset, webOS 4.x) always has it brought to the effective mode,
    sync_debug_status and sync_debug_block."""
    nvm = targets.release(target).nvm
    if nvm.lls:
        prepare_lls(img, target, chip, tree, say, broadcast=broadcast, debug_mode=debug_mode)
        return
    if nvm.records is None:
        _prepare_unrecorded(img, nvm, chip, say, debug_mode, target, tree, broadcast)
        return
    board = board_params(target, tree, broadcast)
    serial = serial or board.get("serialNum", "")
    with open(img, "r+b") as f:
        image = bytearray(f.read(SIZE))
        if len(image) != SIZE:
            raise Error(f"{img}: not {SIZE} bytes")
        if not any(image):
            if chip.image is None:
                say(f"EEPROM blank and not programmed: {chip.note}")
                return
            f.seek(0)
            f.write(chip.image)
            say("EEPROM blank: a new chip (0xFF), programmed as the VM's prepared board data says")
            return
        changed = sync_tool_options(image, nvm, board, say, targets.soc(target).inputs)
        changed = sync_model(image, nvm, board.get("modelName", ""), say) or changed
        changed = sync_serial(image, nvm, serial, say) or changed
        mode = debug_mode if debug_mode is not None else int(board["debugMode"])
        changed = sync_debug_status(image, nvm, mode, say) or changed
        changed = sync_debug_block(image, nvm, mode, say) or changed
        if changed:
            f.seek(0)
            f.write(image)


def factory_chip(fw, nvm, mode):
    """The new chip of a release without records whose factory fields its own code says
    (Nvm.factory: beanjar/nvmfactory.py) with the debug status of mode (DEBUG_MODES' number). The
    debug status's place and values it finds must be the release's (Nvm.debug_status_offset and
    debug_status_values), or Error."""
    from . import nvmfactory

    pak, path = nvm.factory
    r = subprocess.run(
        ["unsquashfs", "-cat", "-no-wildcards", os.path.join(fw, pak + ".pak"), path.lstrip("/")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if r.returncode:
        raise Error(f"{pak}.pak:{path}: {r.stderr.decode(errors='replace').strip() or 'not read'}")
    try:
        offset, release = nvmfactory.fields(r.stdout)["debug"]
        if (offset, release) != (nvm.debug_status_offset, nvm.debug_status_values[2]):
            raise nvmfactory.Error(
                f"its debug status is at 0x{offset:x} up to {release}, not the release's"
                f" 0x{nvm.debug_status_offset or 0:x} up to {nvm.debug_status_values[2]}"
            )
        return nvmfactory.image(
            r.stdout, SIZE, nvm.debug_status_values[mode - DEBUG_MODES["debug"]]
        )
    except (nvmfactory.Error, struct.error, ValueError) as e:
        raise Error(f"EEPROM factory fields from {pak}.pak:{path}: {e}") from None


def _prepare_unrecorded(img, nvm, chip, say, debug_mode, target, tree, broadcast):
    """prepare for a release whose EEPROM the emulator does not program with records (NetCast 4.5,
    SimpleSmart). A blank image (the launcher's new sparse file) becomes a new chip as it leaves
    the factory: chip.image (the factory fields, factory_chip), else every byte 0xFF, and the
    firmware writes its own defaults at its first boot. A programmed one has its debug status
    synced where the release keeps one (sync_debug_status)."""
    with open(img, "r+b") as f:
        image = bytearray(f.read(SIZE))
        if len(image) != SIZE:
            raise Error(f"{img}: not {SIZE} bytes")
        if not any(image):
            f.seek(0)
            if chip.image is not None:
                f.write(chip.image)
                say("EEPROM blank: a new chip (0xFF) with the fields the firmware's factory leaves")
            else:
                f.write(b"\xff" * SIZE)
                say(
                    "EEPROM blank: a new chip (0xFF), not programmed: the firmware writes its own defaults"
                )
            return
        if nvm.debug_status_offset is None:
            return
        mode = (
            debug_mode
            if debug_mode is not None
            else int(board_params(target, tree, broadcast)["debugMode"])
        )
        if sync_debug_status(image, nvm, mode, say):
            f.seek(0)
            f.write(image)


def applied_debug_mode(img, target, tree, broadcast):
    """The debugMode= number the chip image img holds: the one its sidecar img.debugmode says
    prepare_lls last put there, else the board's own (what a chip is made with)."""
    try:
        with open(img + ".debugmode") as f:
            return int(f.read())
    except (OSError, ValueError):
        return int(board_params(target, tree, broadcast)["debugMode"])


def _remember_debug_mode(img, number):
    """Note in img.debugmode that the chip holds debug status number (None: the board's own, no note)."""
    if number is None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(img + ".debugmode")
        return
    with open(img + ".debugmode", "w") as f:
        f.write(f"{number}\n")


def prepare_lls(img, target, chip, tree=paths.TREE, say=print, broadcast=None, debug_mode=None):
    """A blank image of a chip lowlevelstorage owns (Nvm.lls)
    becomes a new chip as a set leaves the factory: chip.image, erased with LLS's own default
    records of the board (new_chip: beanjar/lls.py, which runs the firmware's code on the board's
    command line). A programmed one is LLS's and is left as it is, but for its debug status (the
    system DB's debugstatus item), which follows debug_mode (else the board's debugMode=) as U-Boot
    keeps it in agreement with the kernel's word: the item of chip.records, made for that mode."""
    from . import lls

    with open(img, "r+b") as f:
        image = f.read(SIZE)
        if len(image) != SIZE:
            raise Error(f"{img}: not {SIZE} bytes")
        if any(image):
            if debug_mode is None and not os.path.exists(img + ".debugmode"):
                return  # never set: the chip is the board's own
            wanted = (
                debug_mode
                if debug_mode is not None
                else int(board_params(target, tree, broadcast)["debugMode"])
            )
            if applied_debug_mode(img, target, tree, broadcast) == wanted:
                return
            try:
                new = lls.debug_status_item(image, chip.records or "")
            except lls.Error as e:
                raise Error(f"EEPROM debug status: {e}") from e
            f.seek(0)
            f.write(new)
            _remember_debug_mode(img, debug_mode)
            say(f"EEPROM debug status set: {wanted}")
            return
        if chip.image is None:
            raise Error("EEPROM initialization failed: the VM's prepared board data has no chip")
        f.seek(0)
        f.write(chip.image)
    _remember_debug_mode(img, debug_mode)
    say("EEPROM blank: a new chip (0xFF) with lowlevelstorage's default records of the board")
