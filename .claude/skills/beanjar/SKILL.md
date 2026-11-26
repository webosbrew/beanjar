---
name: beanjar
description: Drive the beanjar CLI to run webOS TV firmware in QEMU VMs — create, start, stop, inspect and screenshot VMs, change their config, reach VMs on other hosts, and run ares-TOOL against them. Use whenever a task involves beanjar, a webOS VM/emulator, booting a firmware/target, or ares-* on a VM. Covers the command shape and conventions; always confirm exact options with --help.
---

# beanjar

`beanjar` runs webOS TV firmware in QEMU VMs. It is verb-first and the options
change often, so **this skill shows the shape and the conventions, not the full
option set — always read `beanjar --help` and `beanjar VERB --help` for the
current options of any command.** Do not memorize flags from here.

## Command shape

```
beanjar [NOUN...] VERB [NAME] [ARGS...] [OPTIONS]
```

- The verb always comes before the VM. VM actions are top-level verbs; everything
  else is a group: `NOUN VERB NAME` (e.g. `config show NAME`, `io tuner map NAME`).
- A group with no verb prints its help and exits 2.
- A target is `NAME`, or `HOST/NAME` to act on a VM on another host (`local/NAME`
  for this machine).
- Global options (`--json`, `--library`, `--repo`) go anywhere on the line.
- `--json` is the stable, machine-readable interface — use it in scripts/tests.
  Human output may change; `--json` output does not.
- Exit status: `0` done, `1` the operation failed, `2` a usage error.

## Discover commands

```
beanjar --help                 # all top-level verbs and groups
beanjar create --help          # options for one verb (authoritative)
beanjar io --help              # a group's verbs; beanjar io tuner --help for one
beanjar config set --help      # a VM's settable config keys
```

## Typical workflows (confirm options with --help)

```
beanjar list                                   # VMs and their state
beanjar create NAME --tag scratch ...          # define a VM on a firmware
beanjar start NAME --reason "<ticket> <what>"  # boot it
beanjar show NAME                              # settings, addresses, SSH, VNC
beanjar screenshot NAME                        # save the TV picture as a PNG
beanjar elements NAME                           # on-screen text/controls (OCR)
beanjar stop NAME                              # graceful power-off (--force if needed)
beanjar config show NAME                       # a VM's kept settings
beanjar config set NAME KEY=VALUE              # change one
beanjar snapshot create NAME SNAP              # save a set-up VM that is off
beanjar snapshot reset NAME SNAP               # put it back before a test run
beanjar delete NAME                            # remove a VM that is off
```

Get a shell on a running VM (the easiest way in — prefer it over ares for quick checks):

```
beanjar shell NAME                             # interactive shell on the VM
beanjar shell NAME -- <command> [args...]      # run one command and return
```

Run ares-TOOL against a VM through beanjar (not a raw ssh/scp):

```
beanjar ares-install -d NAME path/to/app.ipk
beanjar ares-launch  -d NAME <appid>
beanjar ares-shell   -d NAME
```

`ares-TOOL` keeps the webOS SDK's own names and options (the one exception to the
verb-first rule); its ARGS follow the tool after `--`.

## Conventions

- Create scratch VMs with `--tag scratch`; give every `start` a `--reason`.
- One workload per device/VM at a time — serialize installs, launches and
  measurements; never run two against the same target at once.
- Prefer `HOST/NAME` to address a VM on another host rather than connecting to
  that host yourself.
