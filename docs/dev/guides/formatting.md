# Formatting C and Python

Install the developer tools as normal commands on `PATH`, using Python 3.11 or newer.
On Debian:

```sh
sudo apt install clang-format clang-format-19 pipx
pipx install ruff==0.17.0
pipx install pre-commit==4.7.0
pipx ensurepath
# In a new shell, check that clang-format --version reports 19.1.7.
python3 scripts/format-hooks/install-hooks.py
```

The installer sets this checkout's local `core.hooksPath` to a dispatch directory inside
its Git directory. It preserves executable hooks from the previously active hook directory
as symlinks, including the inherited `commit-msg` hook. An inherited `pre-commit` runs first;
the standard pre-commit framework runs next. The original hook directory and global Git
configuration stay untouched. Run the installer again to refresh the formatting wrapper.
To return to an inherited global hooks directory, run `git config --local --unset core.hooksPath`.
If the checkout previously had its own local hooks path, restore that value instead.

```sh
scripts/format all    # Format all tracked files; exits nonzero when files change.
scripts/format check  # Check all tracked files without changing them.
python3 tests/unit/formatting/test_hooks.py
```

Ruff 0.17.0 formats Python with four spaces, a 100-column limit and Python 3.11 syntax
compatibility (the manager and packaging use `tomllib`). No Ruff lint rules run.
clang-format 19.1.7 uses LLVM style, four spaces, Linux function braces and a 100-column
limit. Consecutive `#define` values are aligned. Include sorting is disabled. Ruff's version
is enforced by `[tool.ruff]` in `pyproject.toml`. Use clang-format 19.1.7 to keep C formatting consistent.

Hook entries and `scripts/format` call the installed tools by name. pipx exposes the Python
tools on the user's `PATH` without modifying the system Python environment. The repository
does not need a formatter virtualenv inside its build directory.

pre-commit selects Git-tracked Python files, including Python scripts detected
by their shebang even when they are not executable, and `.c`/`.h` files. Vendor, third-party,
subproject, build and scratch directories and the generated viewer `_core_abi.py` are
excluded. Its cache lives under `build/pre-commit`. Untracked files are not included in the
commands above.

On commit, pre-commit checks staged content. A formatting change blocks the commit;
review the change, stage it and commit again. For partially staged files, pre-commit
temporarily saves unstaged changes and restores them afterward. If restoring them conflicts
with formatter edits, pre-commit rolls back the formatter changes and blocks the commit.
It does not stage formatter changes or discard unstaged edits.

## Before a push

The same installer sets up a `pre-push` hook (`act-check` in `.pre-commit-config.yaml`) that runs
`scripts/act-check`: the public Tests workflow's `lint` and `unit` (amd64) jobs and, when the
commits being pushed touch `guest/`, `meson/` or `packaging/debian/build-deps/`, the Test guest
workflow, each in the CI's `debian:trixie` container through [nektos/act](https://github.com/nektos/act)
(a release binary in `~/.local/bin`, with Docker and Node.js installed). It prints each job's
result and time, and the push fails when a job fails. A run takes about 7 minutes without the
guest build. Skip it with `git push --no-verify`; `scripts/act-check --guest` runs everything by
hand. It runs the working tree, not the pushed commit, and does not cover the arm64 unit entry.
