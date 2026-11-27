# Testing

The tests come in three tiers, by what they need:

| Tier | Directory | Needs | Run by |
|------|-----------|-------|--------|
| unit | `tests/unit/<area>` | nothing built, no firmware, no VM | `pytest tests/unit` |
| integration | `tests/integration/<area>` | this checkout's builds (the host build, a guest build's layers), no firmware VM | `pytest tests/integration`, after the build |
| regress | `tests/regress` | firmware, a VM host, booted VMs | `scripts/regress` ([building](build.md), "Regression runs") |

`meson test -C build/host` runs the unit and integration tiers of the host build (one Meson test per
tier and area, so `--suite unit`, `--suite integration`, `--suite vnc` select them) and the
compiled checks; `meson test -C build/guest-<target>` the guest build's own (the `layers` area). The
tests are pytest's (Debian's `python3-pytest`); `pyproject.toml` at the top of the tree holds its
settings.

## Running

```sh
pytest tests/unit                          # from a fresh clone: no build needed
meson compile -C build/host
pytest tests/integration                   # against build/host
meson test -C build/host                   # both, and the compiled checks
meson test -C build/host --suite unit      # one tier; --suite manager, --suite vnc: one area
pytest tests/unit/manager/test_targets.py -k refused   # one file, some tests
meson test -C build/guest-k8lp-mullet integration-layers
```

The integration tests find the build's programs in `artefacts.json`, which Meson writes into the
build directory (`tests/meson.build`, `build/host/tests/artefacts.json`); `BEANJAR_TEST_ARTEFACTS`
names another one (a guest build's, an arm64 host build's). What a build does not have is skipped,
with the reason (`pytest -ra` lists them). Under Meson, a test whose every case skipped shows as
SKIP, as an exit status 77 does.

`tests/integration/network/test_mitm_addon.py` needs mitmproxy, the developer's own (`pipx install
mitmproxy`, 12.0.1 or newer): Meson runs it through `run-mitm-addon.py` (`integration-mitm-addon`),
with the Python of the mitmweb `mitmtool.py` finds (the interpreter its shebang names), or skips
saying why; under the system Python it is skipped. `test_netns.py` and the web `check-mitm.py` run
that mitmweb and skip without it.

## In CI

The Tests workflow (`.github/workflows/tests.yml`) runs on every pull request and push to main, in
this repository and the public one, with no secrets. Its jobs run `scripts/tests-ci.sh` in a
`debian:trixie` container, as root:

- `lint`: `scripts/format check`, `reuse lint` and `tests/unit/formatting`, with Debian's
  clang-format and reuse and the pinned ruff and pre-commit from pipx (Debian 13 has no ruff, and
  its pre-commit is older than `.pre-commit-config.yaml` asks for).
- `unit`, on amd64 and arm64 natively: role host's build dependencies and the vendored prebuilts,
  as the Packages workflow installs them, a host build, then `meson test --suite unit` (every unit
  area under pytest and the compiled checks), with the failures' logs.

The integration tier is not run there: some of its tests need what a container lacks (a user
systemd, TUN, a writable `/proc/sys`) or more than role host declares. A unit test must therefore
give the same result on any machine, as root too: what it reads of the host (its architecture,
accelerator, CPUs, an installed beanjar) is pinned or mocked, and a tool it lacks is a skip.

To reproduce a job locally, in a throwaway container on a clean clone (it installs packages and
leaves root-owned files in `build/`):

```sh
git clone . /path/to/clone
docker run --rm -v /path/to/clone:/src debian:trixie /src/scripts/tests-ci.sh unit   # or lint
```

## Layout

```
pyproject.toml            tool settings (ruff, pytest)
tests/
  conftest.py          the fixtures: top, artefacts, artefact(name), run_check
  meson.build          the Meson tests of both tiers, artefacts.json
  lib/                 helpers the tests import (testunits, rfbclient, launchkit, ...)
  fakes/               stand-ins the tests run: fake-qemu.py, fake-vm, qemu-system-fake, ...
  unit/<area>/         test_*.py; compiled checks (check-*.c) with tests/unit/meson.build
  integration/<area>/  test_*.py, and the check-*.py programs they run
  regress/             the boot suite's guest scripts (guest.sh, tv.sh), data and app
    lib/               scripts/regress's library: tvsuite, checklist, device_census, teeref
    <area>/            what runs in or against a booted VM: guest programs, guest scripts, VM checks
```

An area is what a test exercises: `manager` (the core and its CLI), `launch` (the host runtime),
`vnc`, `viewer`, `web`, `kernel` (the guest modules' code compiled on the host), `gl`, `packaging`,
and so on. A test goes in the area of the code it tests, in the tier of what it needs.

## Writing a test

- A test module is `test_*.py`. Plain functions with `assert`, fixtures for the setup they share,
  `tmp_path` for files, `pytest.mark.parametrize` for tables. Older files keep `unittest.TestCase`
  classes, which pytest runs as they are.
- Every unit test runs with an empty temporary `HOME` and XDG directories and without `BEANJAR_*`
  variables (`tests/unit/conftest.py`), so the hosts, library and settings of the machine it runs on
  cannot reach it; a test that needs a host or a library creates its own.
- A unit test must pass from a fresh clone: it writes nothing into the checkout and needs nothing
  from `build/`. What it needs from the host (a C compiler, `unsquashfs`, the unpacked kernel
  source) it checks first and skips without.
- An integration test takes what it runs from the `artefact` fixture (`artefact("vnc")`), never from
  a path it guesses. A new artefact is a new entry in `tests/meson.build` (or, for a guest build,
  in the top `meson.build`'s `guest_test_artefacts`).
- A check that is a program of its own (it starts and ends servers and stand-ins, needs a display or
  another Python) stays `check-*.py` beside the test that runs it with `run_check`: its exit status
  77 is a skip, with its last output line as the reason.
- Compiled checks (`check-*.c`) are Meson's: `executable()` and `test()` in the tier's `meson.build`.
- A new area needs its line in `tests/meson.build`'s list of areas (with a timeout), so that Meson
  runs it; pytest finds it by itself.
