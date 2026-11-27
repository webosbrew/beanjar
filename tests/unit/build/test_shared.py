#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""meson/shared.py: one build in a shared directory, reused by every build directory that asks
for it with the same inputs, and done again when an input changes."""

import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SHARED = ROOT / "meson" / "shared.py"


def run(tmp, shared, inputs, dest, after=(), extra=()):
    """shared.py run: the command counts its runs in shared/runs and writes shared/out."""
    command = ["sh", "-c", 'echo x >> "$1/runs"; cat "$2" > "$1/out"', "sh", str(shared), str(inputs[0])]
    args = [sys.executable, str(SHARED), "run", "--dir", str(shared), "--depfile", str(tmp / "d")]
    for a in after:
        args += ["--after", str(a)]
    args += ["--input"] + [str(i) for i in inputs]
    args += ["--file", str(shared / "out"), str(dest)] + list(extra) + ["--"] + command
    return subprocess.run(args, capture_output=True, text=True)


def runs(shared):
    return len((shared / "runs").read_text().split())


def test_key_is_stable_and_names_its_lines():
    out = lambda *lines: subprocess.run([sys.executable, str(SHARED), "key", *lines],
                                        capture_output=True, text=True, check=True).stdout.strip()
    assert out("a", "b") == out("a", "b")
    assert out("a", "b") != out("a", "c")
    assert len(out("a")) == 16


def test_second_build_directory_reuses_the_result(tmp_path):
    shared, src = tmp_path / "shared", tmp_path / "src"
    src.write_text("one\n")
    assert run(tmp_path, shared, [src], tmp_path / "a").returncode == 0
    assert run(tmp_path, shared, [src], tmp_path / "b").returncode == 0
    assert runs(shared) == 1
    assert (tmp_path / "b").read_text() == "one\n"
    assert (tmp_path / "d").read_text().startswith(f"{tmp_path / 'b'}: {shared / '.inputs'}")


def test_changed_input_builds_again(tmp_path):
    shared, src = tmp_path / "shared", tmp_path / "src"
    src.write_text("one\n")
    run(tmp_path, shared, [src], tmp_path / "a")
    src.write_text("two\n")
    assert run(tmp_path, shared, [src], tmp_path / "a").returncode == 0
    assert runs(shared) == 2
    assert (tmp_path / "a").read_text() == "two\n"


def test_missing_output_builds_again(tmp_path):
    shared, src = tmp_path / "shared", tmp_path / "src"
    src.write_text("one\n")
    run(tmp_path, shared, [src], tmp_path / "a")
    (shared / "out").unlink()
    run(tmp_path, shared, [src], tmp_path / "b")
    assert runs(shared) == 2


def test_failed_command_is_not_reused(tmp_path):
    shared, src = tmp_path / "shared", tmp_path / "src"
    src.write_text("one\n")
    args = [sys.executable, str(SHARED), "run", "--dir", str(shared), "--input", str(src),
            "--file", str(shared / "out"), str(tmp_path / "a"), "--", "sh", "-c", f"touch {shared}/out; exit 3"]
    assert subprocess.run(args).returncode == 3
    assert not (shared / ".inputs").exists()


def test_after_directory_changes_the_inputs(tmp_path):
    base, shared, src = tmp_path / "base", tmp_path / "shared", tmp_path / "src"
    src.write_text("one\n")
    run(tmp_path, base, [src], tmp_path / "base-out")
    run(tmp_path, shared, [src], tmp_path / "a", after=[base])
    run(tmp_path, shared, [src], tmp_path / "a", after=[base])
    assert runs(shared) == 1
    (base / ".inputs").write_text("other\n")
    run(tmp_path, shared, [src], tmp_path / "a", after=[base])
    assert runs(shared) == 2


def test_unbuilt_after_directory_fails(tmp_path):
    shared, src = tmp_path / "shared", tmp_path / "src"
    src.write_text("one\n")
    r = run(tmp_path, shared, [src], tmp_path / "a", after=[tmp_path / "base"])
    assert r.returncode != 0 and "has not been built" in r.stderr


def test_tree_is_copied_and_kept_when_unchanged(tmp_path):
    shared, src = tmp_path / "shared", tmp_path / "src"
    src.write_text("one\n")
    (shared / "tree" / "sub").mkdir(parents=True)
    (shared / "tree" / "sub" / "f").write_text("f\n")
    (shared / "tree" / "link").symlink_to("sub/f")
    extra = ["--tree", str(shared / "tree"), str(tmp_path / "tree")]
    run(tmp_path, shared, [src], tmp_path / "a", extra=extra)
    assert (tmp_path / "tree" / "sub" / "f").read_text() == "f\n"
    assert (tmp_path / "tree" / "link").readlink() == Path("sub/f")
    inode = (tmp_path / "tree").stat().st_ino
    run(tmp_path, shared, [src], tmp_path / "a", extra=extra)
    assert (tmp_path / "tree").stat().st_ino == inode


def test_concurrent_build_directories_build_once(tmp_path):
    shared, src = tmp_path / "shared", tmp_path / "src"
    src.write_text("one\n")
    results = []
    threads = [threading.Thread(target=lambda i=i: results.append(
        run(tmp_path, shared, [src], tmp_path / f"o{i}").returncode)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [0] * 4
    assert runs(shared) == 1
