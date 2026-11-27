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
compatibility (the manager and packaging use `tomllib`). The only lint rules are the import ones
(`[tool.ruff.lint]`: `I`, sorted imports, and `F401`, no unused imports), run by the same hook and
by `scripts/format check`: the commit hook fixes them, and an import kept on purpose, for its side
effect or as a re-export, gets `# noqa: F401` and a few words why.
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

Push `main` to GitHub with `scripts/push-main [--guest] [COMMIT [REMOTE]]` (HEAD and `origin` by
default). It runs `scripts/act-check` on the commit, in a throwaway clone that is removed
afterwards, so uncommitted files in the working tree do not matter: the public Tests workflow's
`lint` and `unit` (amd64) jobs and, when the commit changes `guest/`, `meson/` or
`packaging/debian/build-deps/` against the remote's `main` (or with `--guest`), the Test guest
workflow, each in the CI's `debian:trixie` container through
[nektos/act](https://github.com/nektos/act) (a release binary in `~/.local/bin`, with Docker and
Node.js installed). It prints each job's result and time. A run takes about 7 minutes without the
guest build (about 16 with it). When the jobs pass it writes a stamp `build/act/passed/<sha>` and
runs `git push <remote> <sha>:refs/heads/main`; when one fails it pushes nothing.

The check is not a pre-push hook because git opens the connection before it runs a pre-push hook,
and GitHub drops the idle connection during the minutes the check takes. The `pre-push` hook that
the installer sets up only looks for the stamp: a push of `main` to a GitHub URL without a stamp
for the pushed commit is refused with a line naming `scripts/push-main`. Other branches and other
remotes are not held up. `git push --no-verify` skips the hook.

`scripts/act-check` run by hand checks the working tree (`--guest` also runs the Test guest
workflow); its output says which it checked.

The jobs run on a temporary copy of each workflow with only the container image replaced, by
`beanjar-act-node:trixie`: the stock `debian:trixie` plus this machine's `node`, which the node-based
actions (cache, upload-artifact) need and the real runner supplies. That is the only difference
from the CI's container; the stock `debian:trixie` image is not touched. The arm64 unit entry is
not covered. act's artifact server cannot take the workflows' uploads, so a failed upload does not
fail a job and no artifacts are kept.
