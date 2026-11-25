# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""A target's firmware facts: the few things the guest build asks of a firmware, kept in the tree
as guest/targets/facts/<chip>-<release>.txt so the build needs no firmware (docs/dev/guides/build.md,
"Firmware facts").

One fact per line, the lines sorted; '#' starts a comment line:

  path PATH yes|no          whether the firmware's rootfs image has PATH (a rule's question, e.g.
                            usr/lib/nyx/modules/nyxDrm.module)
  version PATH SUFFIX|none  the version suffix of the file the library name PATH resolves to in the
                            rootfs (usr/lib/libwayland-client.so.0 -> libwayland-client.so.0.20.0:
                            20.0), "-" for a plain file, none without one
  lib PATH SONAME           a firmware library the guest code links against, and its SONAME
  sym PATH NAME[@VERSION] TYPE [SIZE]
                            a symbol of library PATH that the guest code uses: its default version
                            where the library versions it, its type (func, object, tls, notype) and,
                            for data, its size
  defines IMAGE:PATH:NAME yes|no
                            whether the library PATH of the firmware's IMAGE (rootfs or bsppart: the
                            image's own path, bsp/usr/lib/... in bsppart) defines the dynamic symbol
                            NAME; no where it has no such file (a rule's question about a stock
                            library the build does not link, e.g. what the HAL below a shim reaches)
  exports PATH:NAME=VALUE yes|no
                            whether the shell file PATH of the rootfs image exports NAME as VALUE
                            (a line "export NAME=VALUE", the value quoted or not); no where it has
                            no such file (a rule's question about what the firmware's environment
                            selects, e.g. the compositor's QT_QPA_EGLFS_INTEGRATION)

Nothing else of the firmware: the build links against stub libraries generated from the lib and sym
lines (stub_source; `python3 -m beanjar.fwfacts stub`, build_main), with the library's SONAME and only those symbols, so the
binaries it makes record the same DT_NEEDED entries and symbol versions as linking the firmware's
own files did.

The questions come from the build. A guest build writes the ones it asked to
firmware-questions.txt in its build directory (guest/targets/meson.build), and stops when the
facts file does not answer one. generate() answers them from a firmware's paks, read in place (the
rootfs image mounted with squashfuse, single files with unsquashfs -cat): the path, version
and lib questions are the build directory's (else the facts file's own); the symbols are the global
names of the object files of the build's links against a firmware library, and of their outputs once
linked (an executable's own _init or _end, exported where the library defines them too), that the
library defines (else the facts file's own symbols, looked up again).
`host/scripts/vm-launcher firmware-facts` is the command; --check reports a facts file that differs
from what the firmware answers.
"""

import contextlib
import os
import re
import subprocess
import tempfile

FACTS_DIR = ("guest", "targets", "facts")
QUESTIONS = "firmware-questions.txt"
KINDS = ("defines", "exports", "lib", "path", "sym", "version")
IMAGES = ("rootfs", "bsppart")
TYPES = {"FUNC": "func", "OBJECT": "object", "TLS": "tls", "NOTYPE": "notype"}
SIZED = ("object", "tls")
ASM_TYPES = {"func": "function", "object": "object", "tls": "tls_object"}


class Error(Exception):
    pass


# ---------------------------------------------------------------- the file format -------------


def parse(text, where="facts"):
    """{'path': {PATH: bool}, 'version': {PATH: str}, 'lib': {PATH: SONAME},
    'sym': {PATH: {NAME: (version or None, type, size or None)}}, 'defines': {IMAGE:PATH:NAME: bool},
    'exports': {PATH:NAME=VALUE: bool}} of a facts file's text. Error naming the line for anything
    else."""
    facts = {"path": {}, "version": {}, "lib": {}, "sym": {}, "defines": {}, "exports": {}}
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        w = line.split()
        bad = Error(f"{where}:{n}: not a fact: {line!r}")
        if w[0] == "path" and len(w) == 3 and w[2] in ("yes", "no"):
            facts["path"][w[1]] = w[2] == "yes"
        elif w[0] == "defines" and len(w) == 3 and w[2] in ("yes", "no") and _defines_q(w[1]):
            facts["defines"][w[1]] = w[2] == "yes"
        elif w[0] == "exports" and len(w) == 3 and w[2] in ("yes", "no") and _exports_q(w[1]):
            facts["exports"][w[1]] = w[2] == "yes"
        elif w[0] == "version" and len(w) == 3:
            facts["version"][w[1]] = w[2]
        elif w[0] == "lib" and len(w) == 3:
            facts["lib"][w[1]] = w[2]
        elif w[0] == "sym" and len(w) in (4, 5) and w[3] in TYPES.values():
            sized = w[3] in SIZED
            if sized != (len(w) == 5) or (sized and not w[4].isdigit()):
                raise bad
            name, _, version = w[2].partition("@")
            facts["sym"].setdefault(w[1], {})[name] = (
                version or None,
                w[3],
                int(w[4]) if sized else None,
            )
        else:
            raise bad
    for lib in facts["sym"]:
        if lib not in facts["lib"]:
            raise Error(f"{where}: symbols of {lib}, which has no lib line")
    return facts


def lines(facts):
    """The fact lines of facts (parse's form), sorted."""
    out = [f"path {p} {'yes' if v else 'no'}" for p, v in facts["path"].items()]
    out += [f"defines {q} {'yes' if v else 'no'}" for q, v in facts["defines"].items()]
    out += [f"exports {q} {'yes' if v else 'no'}" for q, v in facts["exports"].items()]
    out += [f"version {p} {v}" for p, v in facts["version"].items()]
    out += [f"lib {p} {s}" for p, s in facts["lib"].items()]
    for lib, syms in facts["sym"].items():
        for name, (version, kind, size) in syms.items():
            out.append(
                f"sym {lib} {name}{'@' + version if version else ''} {kind}"
                + (f" {size}" if kind in SIZED else "")
            )
    return sorted(out)


def format_facts(target, facts, firmware_name=None):
    """A facts file's text: a header naming the target (and the firmware it was read from), then lines()."""
    head = [
        f"# {target}: what the guest build asks of its firmware (beanjar/fwfacts.py).",
        "# Generated: host/scripts/vm-launcher firmware-facts "
        + target
        + (f" (from {firmware_name})" if firmware_name else ""),
    ]
    return "\n".join(head + lines(facts)) + "\n"


def _defines_q(q):
    """Whether q is a defines question's IMAGE:PATH:NAME."""
    w = q.split(":")
    return len(w) == 3 and w[0] in IMAGES and all(w[1:])


def _exports_q(q):
    """Whether q is an exports question's PATH:NAME=VALUE."""
    path, _, assign = q.partition(":")
    name, eq, _value = assign.partition("=")
    return bool(path) and bool(eq) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is not None


QUESTION_KINDS = ("path", "version", "lib", "defines", "exports")


def parse_questions(text):
    """{'path': [...], 'version': [...], 'lib': [...], 'defines': [...], 'exports': [...]} of a
    build's firmware-questions.txt (one "KIND PATH" per line; a defines question's PATH is
    IMAGE:PATH:NAME, an exports question's PATH:NAME=VALUE)."""
    q = {k: set() for k in QUESTION_KINDS}
    for line in text.splitlines():
        w = line.split()
        if not w:
            continue
        if (
            len(w) != 2
            or w[0] not in q
            or (w[0] == "defines" and not _defines_q(w[1]))
            or (w[0] == "exports" and not _exports_q(w[1]))
        ):
            raise Error(f"{QUESTIONS}: not a question: {line!r}")
        q[w[0]].add(w[1])
    return {k: sorted(v) for k, v in q.items()}


def unanswered(facts, questions):
    """The questions (parse_questions' form) facts does not answer, as "KIND PATH" strings."""
    return [f"{k} {p}" for k in QUESTION_KINDS for p in questions.get(k, ()) if p not in facts[k]]


# ---------------------------------------------------------------- stub libraries --------------


def _asm_name(name):
    return name if re.fullmatch(r"[A-Za-z_.$][A-Za-z0-9_.$]*", name) else '"' + name + '"'


def stub_source(syms):
    """The assembly of a stub library defining syms ({NAME: (version, type, size)}): every symbol
    global, of its type, data of its size (zero filled). No code runs: the stub only stands in at
    link time."""
    out = []
    for name in sorted(syms):
        _version, kind, size = syms[name]
        n = _asm_name(name)
        section = {
            "func": "\t.text",
            "object": "\t.data",
            "notype": "\t.data",
            "tls": '\t.section .tdata,"awT",%progbits',
        }[kind]
        out += [section, f"\t.globl {n}"]
        if kind != "notype":
            out.append(f"\t.type {n}, %{ASM_TYPES[kind]}")
        if kind in SIZED:
            out += [f"\t.size {n}, {size}", f"{n}:", f"\t.zero {size}" if size else ""]
        else:
            out.append(f"{n}:")
    return "\n".join(line for line in out if line) + "\n"


def stub_version_script(syms):
    """A version script giving each versioned symbol of syms its version, or None when none is
    versioned (an unversioned library)."""
    nodes = {}
    for name, (version, _kind, _size) in syms.items():
        if version:
            nodes.setdefault(version, []).append(name)
    if not nodes:
        return None
    return "".join(
        f"{v} {{\n  global:\n" + "".join(f"    {_quote_vs(n)};\n" for n in sorted(names)) + "};\n"
        for v, names in sorted(nodes.items())
    )


def _quote_vs(name):
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) else '"' + name + '"'


def build_stub(facts, lib, cc, out, workdir):
    """Compile the stub of firmware library lib (a path in facts' lib lines) to out with cc (the
    compiler command, a list): a shared library with lib's SONAME and its symbols, needing nothing."""
    if lib not in facts["lib"]:
        raise Error(f"no lib line for {lib}")
    syms = facts["sym"].get(lib, {})
    os.makedirs(workdir, exist_ok=True)
    base = os.path.join(workdir, os.path.basename(out))
    with open(base + ".S", "w") as f:
        f.write(stub_source(syms))
    argv = list(cc) + [
        "-shared",
        "-nostdlib",
        "-Wl,-soname," + facts["lib"][lib],
        "-Wl,--hash-style=both",
        "-o",
        out,
        base + ".S",
    ]
    script = stub_version_script(syms)
    if script:
        with open(base + ".map", "w") as f:
            f.write(script)
        argv.insert(-1, "-Wl,--version-script=" + base + ".map")
    r = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if r.returncode != 0:
        raise Error(f"the stub of {lib} does not build:\n{r.stdout}")


# ---------------------------------------------------------------- reading ELF files -----------

_SYM = re.compile(
    r"^\s*\d+:\s+[0-9a-fA-F]+\s+(?P<size>\d+|0x[0-9a-fA-F]+)\s+(?P<type>\S+)\s+(?P<bind>\S+)\s+\S+\s+"
    r"(?P<ndx>\S+)\s+(?P<name>\S+)"
)


def _readelf(argv):
    r = subprocess.run(
        ["readelf", "-W"] + argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
    )
    if r.returncode != 0:
        raise Error(f"readelf {' '.join(argv)}: {r.stderr.strip()}")
    return r.stdout


def exports(text):
    """{NAME: (version or None, type, size)} of the symbols `readelf -W --dyn-syms` text says a
    library defines: global and weak, at their default version (NAME@@V) or unversioned; a
    non-default version (NAME@V) only where the name has no default one."""
    out, hidden = {}, {}
    for line in text.splitlines():
        m = _SYM.match(line)
        if not m or m["ndx"] == "UND" or m["bind"] not in ("GLOBAL", "WEAK", "UNIQUE"):
            continue
        name = re.sub(r"\s*\(\d+\)$", "", m["name"])
        kind = TYPES.get(m["type"])
        if kind is None:
            continue
        size = int(m["size"], 0) if kind in SIZED else None
        if "@@" in name:
            name, version = name.split("@@", 1)
            out[name] = (version, kind, size)
        elif "@" in name:
            name, version = name.split("@", 1)
            hidden.setdefault(name, (version, kind, size))
        else:
            out.setdefault(name, (None, kind, size))
    for name, v in hidden.items():
        out.setdefault(name, v)
    return out


def global_names(text):
    """The names of the global and weak symbols in `readelf -W -s` text (of objects, archives or a
    linked file), defined or not: what a link resolves against a library, and what it defines that a
    library on its link line also defines, which it then exports (an executable's _init, _end, ...)."""
    out = set()
    for line in text.splitlines():
        m = _SYM.match(line)
        if m and m["bind"] in ("GLOBAL", "WEAK", "UNIQUE") and m["type"] not in ("SECTION", "FILE"):
            out.add(m["name"].split("@", 1)[0])
    return out


def soname(text):
    """The SONAME in `readelf -d` text, or None."""
    m = re.search(r"\(SONAME\)\s+Library soname: \[(.*)\]", text)
    return m.group(1) if m else None


# ---------------------------------------------------------------- a build's link steps --------


def link_steps(ninja_text):
    """[(output, explicit inputs, the words of LINK_ARGS)] of every link step (a rule *_LINKER) in
    a build.ninja's text."""
    text = ninja_text.replace("$\n", "")
    steps, current = [], None
    for line in text.splitlines():
        if line.startswith("build "):
            current = None
            head = re.split(r"(?<!\$):", line[len("build ") :], maxsplit=1)
            if len(head) != 2:
                continue
            words = _ninja_words(head[1])
            if words and words[0].endswith("_LINKER"):
                explicit = []
                for w in words[1:]:
                    if w in ("|", "||", "|@"):
                        break
                    explicit.append(w)
                current = (_ninja_words(head[0])[0], explicit, [])
                steps.append(current)
        elif current is not None and line.startswith(" LINK_ARGS = "):
            current[2].extend(_ninja_words(line[len(" LINK_ARGS = ") :]))
        elif not line.startswith(" "):
            current = None
    return steps


def _ninja_words(s):
    s = s.replace("$ ", "\0").replace("$:", ":").replace("$$", "$")
    return [w.replace("\0", " ") for w in s.split()]


def used_symbols(build_dir, libs):
    """{lib: {NAME, ...}} for each lib (paths in the rootfs) linked by a link step of build_dir: the
    global names of its object files and archives, and of its output once linked (global_names). A
    step links lib when a word of its LINK_ARGS ends in lib's file name (the firmware's file before,
    its stub now)."""
    ninja = os.path.join(build_dir, "build.ninja")
    with open(ninja) as f:
        steps = link_steps(f.read())
    out = {lib: set() for lib in libs}
    for output, inputs, args in steps:
        linked = [lib for lib in libs if any(a.endswith("/" + os.path.basename(lib)) for a in args)]
        if not linked:
            continue
        files = [os.path.join(build_dir, i) for i in inputs if i.endswith((".o", ".a"))]
        missing = [f for f in files if not os.path.isfile(f)]
        if missing:
            raise Error(
                f"{build_dir}: not compiled yet ({missing[0]}): meson compile -C {build_dir} first"
            )
        if os.path.isfile(os.path.join(build_dir, output)):
            files.append(os.path.join(build_dir, output))
        names = global_names(_readelf(["-s"] + files)) if files else set()
        for lib in linked:
            out[lib] |= names
    return out


# ---------------------------------------------------------------- answering from a firmware ---


def _image(fw_dir, image):
    """(path, offset) of fw_dir's stock image (rootfs or bsppart): <image>.pak (root.pak for
    SimpleSmart's rootfs), or super.pak's logical image. Error without it."""
    from . import paths, superpak

    try:
        logical = superpak.images(fw_dir)
    except ValueError as e:
        raise Error(str(e)) from None
    if image in logical:
        return logical[image][:2]
    src = paths.root_pak(fw_dir) if image == "rootfs" else None
    src = src or os.path.join(fw_dir, image + ".pak")
    if not os.path.isfile(src):
        raise Error(f"{fw_dir}: no {image}.pak or super.pak {image}")
    return src, 0


@contextlib.contextmanager
def mounted_root(fw_dir):
    """The firmware's rootfs image mounted read-only with squashfuse for the with block, so the
    facts' rootfs paths are read in place without extracting it; unmounted after. A fw_dir that
    already shows the image as files (an epkfs 0.3+ mount: paths.expanded_image) is read as it is,
    with no mount."""
    from . import paths

    root = paths.expanded_image(fw_dir)
    if root:
        yield root
        return
    src, offset = _image(fw_dir, "rootfs")
    mnt = tempfile.mkdtemp(prefix="beanjar-fwfacts-")
    try:
        r = subprocess.run(
            ["squashfuse", "-o", f"ro,offset={offset}", src, mnt],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        if r.returncode != 0:
            raise Error(f"{src}: squashfuse cannot mount it: {r.stderr.strip()}")
        try:
            yield mnt
        finally:
            subprocess.run(["fusermount3", "-u", mnt], check=True)
    finally:
        os.rmdir(mnt)


def _in_root(root, path):
    """path (in the rootfs) in root, its last component's symlinks followed inside root as the
    guest would (a library's name links to its versioned file)."""
    rel = path.strip("/")
    for _ in range(40):
        full = os.path.join(root, rel)
        if not os.path.islink(full):
            return full
        target = os.readlink(full)
        rel = os.path.normpath(
            target.lstrip("/")
            if target.startswith("/")
            else os.path.join(os.path.dirname(rel), target)
        )
        if rel.startswith(".."):
            raise Error(f"{path}: a symlink out of the root")
    raise Error(f"{path}: a symlink loop")


def answer_version(root, path):
    """The version suffix of the file path resolves to (libwayland-client.so.0 ->
    libwayland-client.so.0.20.0: 20.0), "-" when it is that file itself, none without it."""
    full = _in_root(root, path)
    if not os.path.isfile(full):
        return "none"
    name, base = os.path.basename(full), os.path.basename(path)
    if name == base:
        return "-"
    if not name.startswith(base + "."):
        raise Error(f"{path} resolves to {name}, not a version of it")
    return name[len(base) + 1 :]


def image_file(fw_dir, image, path):
    """The bytes of path in fw_dir's stock image (rootfs or bsppart: <image>.pak, or super.pak's
    logical image), its symlinks followed by unsquashfs; None where the image has no such file.
    Error without the image. Read as files where fw_dir already shows the image so
    (paths.expanded_image), without unsquashfs."""
    from . import paths

    root = paths.expanded_image(fw_dir, image)
    if root:
        try:
            full = _in_root(root, path)
            with open(full, "rb") as f:
                return f.read() or None
        except OSError:
            return None
    src, offset = _image(fw_dir, image)
    r = subprocess.run(
        ["unsquashfs", "-cat", "-no-wildcards", "-o", str(offset), src, path.lstrip("/")],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return r.stdout if r.returncode == 0 and r.stdout else None


def answer_defines(fw_dir, q):
    """A defines question's answer: whether the library of q (IMAGE:PATH:NAME) defines NAME."""
    image, path, name = q.split(":")
    data = image_file(fw_dir, image, path)
    if data is None:
        return False
    with tempfile.NamedTemporaryFile(suffix=".so") as f:
        f.write(data)
        f.flush()
        return name in exports(_readelf(["--dyn-syms", f.name]))


def answer_exports(fw_dir, q):
    """An exports question's answer: whether the rootfs shell file of q (PATH:NAME=VALUE) has a line
    "export NAME=VALUE", the value bare or in single or double quotes."""
    path, _, assign = q.partition(":")
    name, _, value = assign.partition("=")
    data = image_file(fw_dir, "rootfs", path)
    if data is None:
        return False
    v = re.escape(value)
    line = rf"[ \t]*export[ \t]+{re.escape(name)}=(?:{v}|\"{v}\"|'{v}')[ \t]*(?:#.*)?"
    return any(re.fullmatch(line, s) for s in data.decode("utf-8", "replace").splitlines())


def answer(fw_dir, questions, symbols, entries=None):
    """The facts (parse's form) of the firmware in fw_dir for questions (parse_questions' form) and
    symbols ({lib: names}). entries: the rootfs image's entries in the questions' directories
    (default: paths.rootfs_entries, read from the image as the build always did)."""
    facts = {"path": {}, "version": {}, "lib": {}, "sym": {}, "defines": {}, "exports": {}}
    for q in questions.get("defines", ()):
        facts["defines"][q] = answer_defines(fw_dir, q)
    for q in questions.get("exports", ()):
        facts["exports"][q] = answer_exports(fw_dir, q)
    if questions["path"]:
        if entries is None:
            from . import paths

            dirs = sorted({os.path.dirname(p) for p in questions["path"]})
            try:
                entries = paths.rootfs_entries(fw_dir, dirs)
            except ValueError as e:
                raise Error(str(e)) from None
        have = set(entries)
        facts["path"] = {p: p in have for p in questions["path"]}
    if questions["version"] or questions["lib"]:
        with mounted_root(fw_dir) as root:
            _answer_libs(root, questions, symbols, facts)
    return facts


def _answer_libs(root, questions, symbols, facts):
    """The version, lib and sym facts, read in root (the mounted rootfs image)."""
    for p in questions["version"]:
        facts["version"][p] = answer_version(root, p)
    for lib in questions["lib"]:
        full = _in_root(root, lib)
        if not os.path.isfile(full):
            raise Error(f"the firmware has no {lib}, which the build links against")
        name = soname(_readelf(["-d", full]))
        if not name:
            raise Error(f"{lib} has no SONAME")
        facts["lib"][lib] = name
        defined = exports(_readelf(["--dyn-syms", full]))
        used = {n: defined[n] for n in symbols.get(lib, ()) if n in defined}
        if used:
            facts["sym"][lib] = used


def facts_path(top, target):
    return os.path.join(top, *FACTS_DIR, target + ".txt")


def generate(top, target, fw_dir, build_dir=None, facts_file=None, firmware_name=None):
    """The text of target's facts file, read from fw_dir (see the module's docstring for where the
    questions come from)."""
    facts_file = facts_file or facts_path(top, target)
    old = None
    if os.path.isfile(facts_file):
        with open(facts_file) as f:
            old = parse(f.read(), facts_file)
    qfile = os.path.join(build_dir, QUESTIONS) if build_dir else None
    if qfile and os.path.isfile(qfile):
        with open(qfile) as f:
            questions = parse_questions(f.read())
    elif old is not None:
        questions = {k: sorted(old[k]) for k in QUESTION_KINDS}
    else:
        raise Error(
            f"no {facts_file} and no {QUESTIONS} to answer: meson setup the target's guest build "
            "directory first and give it with --build"
        )
    if build_dir and os.path.isfile(os.path.join(build_dir, "build.ninja")):
        symbols = used_symbols(build_dir, questions["lib"])
    else:
        symbols = {lib: set((old or {}).get("sym", {}).get(lib, {})) for lib in questions["lib"]}
    facts = answer(fw_dir, questions, symbols)
    return format_facts(target, facts, firmware_name)


def main(argv=None):
    """host/scripts/vm-launcher firmware-facts TARGET [--firmware DIR] [--build DIR] [--check]"""
    import argparse

    p = argparse.ArgumentParser(prog="vm-launcher firmware-facts")
    add_arguments(p)
    return run(p.parse_args(argv))


def add_arguments(p):
    p.add_argument("target", help="<chip>-<release>")
    p.add_argument(
        "--firmware",
        help="the firmware directory, its paks read in place (default: the target's, config FW --target)",
    )
    p.add_argument(
        "--build",
        help="the target's guest build directory: its questions and, once compiled, the symbols its links use",
    )
    p.add_argument(
        "--check", action="store_true", help="report a facts file that differs; write nothing"
    )
    p.add_argument("--output", help="the facts file (default guest/targets/facts/TARGET.txt)")


def run(a):
    from . import paths, targets

    top = paths.TREE.top
    target = targets.by_name(a.target)
    if target is None:
        raise Error(f"{a.target}: not a target this tree boots (config TARGETS)")
    if a.firmware:
        fw = os.path.abspath(a.firmware)
    else:
        try:
            fw, _name = paths.TREE.target_firmware(target)
        except ValueError as e:
            raise Error(str(e)) from None
    out = a.output or facts_path(top, a.target)
    text = generate(top, a.target, fw, a.build, out, os.path.basename(fw.rstrip("/")))
    current = open(out).read() if os.path.isfile(out) else None
    if a.check:
        if current == text:
            print(f"{out}: up to date")
            return 0
        import difflib

        print(f"{out}: differs from {fw}:")
        print(
            "".join(
                difflib.unified_diff(
                    (current or "").splitlines(True), text.splitlines(True), out, "firmware"
                )
            ),
            end="",
        )
        return 1
    if current != text:
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w") as f:
            f.write(text)
        print(f"{out}: written")
    else:
        print(f"{out}: up to date")
    return 0


def build_main(argv=None):
    """python3 -m beanjar.fwfacts: stub FACTS LIB OUT -- CC...  |  questions OUT [QUESTION...]

    The guest build's command line (guest/targets/meson.build): the stub library of firmware library
    LIB (a lib line's path) compiled with CC to OUT, or the build's questions ("KIND PATH") written
    to OUT, rewritten only when they change."""
    import sys

    argv = sys.argv[1:] if argv is None else argv
    try:
        if argv and argv[0] == "stub" and "--" in argv:
            i = argv.index("--")
            facts_file, lib, out = argv[1:i]
            with open(facts_file) as f:
                facts = parse(f.read(), facts_file)
            build_stub(facts, lib, argv[i + 1 :], out, out + ".src")
            return 0
        if argv and argv[0] == "questions":
            text = "".join(q + "\n" for q in sorted(set(argv[2:])))
            current = open(argv[1]).read() if os.path.isfile(argv[1]) else None
            if current != text:
                with open(argv[1], "w") as f:
                    f.write(text)
            return 0
    except (Error, OSError) as e:
        print(f"fwfacts: {e}", file=sys.stderr)
        return 1
    print("usage: " + build_main.__doc__.split("\n")[0].split(": ", 1)[1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(build_main())
