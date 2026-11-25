# Apps

A VM installs and runs apps as a TV does: through Developer Mode with the webOS SDK tools, or
through Homebrew Channel as on a rooted TV. Both are guest layers, chosen while the VM is off.

## Developer Mode and the ares tools

The `devmode` layer makes the VM a TV with Developer Mode switched on: its SSH server for
`prisoner` on port 9922 and its key server on 9991, with the VM's own developer key.

```sh
beanjar config set tv1 layers=normal,gl,devmode
beanjar start tv1
beanjar ares-install -d tv1 com.example.app_1.0.0_all.ipk
beanjar ares-launch -d tv1 com.example.app
beanjar ares-shell -d tv1
```

`beanjar ares-TOOL -d NAME` runs any webOS SDK tool against that one VM, with a device entry made
for it each time; your own `~/.webos` is never read or written. Install the tools yourself, either
the Rust [ares-cli](https://github.com/webosbrew/ares-cli-rs) or LG's official Node.js CLI; Beanjar
looks for `ares-TOOL` on `PATH`, then in `~/.cargo/bin`. A VM on another host works the same way
(`-d HOST/NAME`), with the tool running on this machine.

The layer has no developer.lge.com session, so apps that read Developer Mode's session token or
its expiry report it as unavailable. Installing and launching apps does not need it.

## Homebrew Channel

The `hbchannel` layer installs [Homebrew Channel](https://github.com/webosbrew/webos-homebrew-channel)
elevated, as on a rooted TV. It needs `devmode` and replaces `rootssh`:

```sh
beanjar config set tv1 layers=normal,gl,devmode,hbchannel
```

`hbchannel` is the default release (0.8.0); `hbchannel@0.7.3` picks the other one built.
Homebrew Channel can update itself in the VM, and its newer copy then stays until `beanjar wipe`.
See [Homebrew Channel](../../host/core/README.md#homebrew-channel) in the manager guide.

## Root

With `rootssh` or `hbchannel`, root SSH listens on the VM's port 6622 with the VM's own keys;
`beanjar show tv1` prints the `ssh` command, which uses the VM's own client key. Your own SSH keys
are not given to the guest unless you add them (`beanjar identity authorized-keys add tv1 --mine`):
the guest can read every key it gets, and your keys identify you. `beanjar identity
authorized-keys show tv1` lists the keys it gets, and `... reset tv1` takes yours out again. The ares tools run as root with `--root`:

```sh
beanjar ares-shell --root -d tv1 -r 'id'
beanjar ares-push --root -d tv1 script.sh /tmp/
```

Without any layer, `beanjar shell` is a root shell inside webOS over the VM's own shell port (no
SSH, developer mode or network), or runs one command with its exit status:

```sh
beanjar shell tv1
beanjar shell tv1 -- uname -a
cat app.ipk | beanjar shell tv1 -- sh -c 'cat > /tmp/app.ipk'
```

Whoever can open the VM's `run/shell.sock` (its owner) is root in it, as with the serial console.
Details: [the VM manager](../../host/core/README.md#shell).

## Second-screen apps and other machines

An app on a phone or another PC reaches the VM's TV services (SSAP on ports 3000 and 3001) at the
VM's address. For a client on another machine, make the VM listen there
(`beanjar config set tv1 listen=0.0.0.0`) or put it on the LAN (`link=lan`); see
[Network](vms.md#network). SSAP pairs a client only when it is accepted on the TV's screen, as on
a TV.

Protected video (Widevine and PlayReady) is outside Beanjar's scope. LG's servers accept a VM
only with a real model key of a matching model; see [Identity](vms.md#identity).
