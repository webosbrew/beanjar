# Per-VM identity

Each VM has its own board MAC, serial and generated credentials. Firmware paks and shared
guest/kernel/initramfs artifacts contain no identity for an installation. The library owns the proxy
CA shared by its VMs.

## Storage and ownership

The VM's configuration stores its MAC and serial. Its `identity/` directory holds generated
credentials and provisioning inputs. The launcher builds the per-VM `vm` layer from those
inputs and stacks it above the other layers; files not needed by the guest remain host-side.

A newly created or copied VM receives its own identity. Changing configuration and regenerating
identity are distinct operations: firmware and state may retain old values until state is reset.
The model and region are the board's, not the VM's: a VM boots its chip's default board, a US
model where the firmware ships on US models, unless its `broadcast` setting chooses another
([board profiles](../../user/board-profiles.md)). A board change does not rewrite the EEPROM's
factory records or the country chosen in first use; wipe the state to adopt it.
The manager's `identity renew` workflow handles the identity and state together; see the
[manager guide](../../../host/core/README.md).

## Device identity and model key

The MAC and serial distinguish VMs, as does the LGUDID, LG's unique device ID: a set's factory
writes it with the MAC into the eMMC's macadr partition, where securitymanager and, from queue on,
libsecretagent read it. The VM's is generated once, as the factory does, and kept in `identity/`;
the launcher presents the partition built from it ([the LGUDID](../../research/lgudid.md)), so
it is the same across processes and boots and survives a wipe. A model key is a separate provisioning input used by LG's
identity/authentication algorithms; a synthetic key does not authenticate to LG's servers.
No key material is distributed with the project. 

SSH host keys, user authentication keys and the Developer Mode passphrase are also VM-specific.
Guest exposure follows the selected platform's supported layers; it is not a promise that
webOS Developer Mode exists on the legacy platforms.

## Proxy CA

The library CA is installed into the guest trust stores declared by the target. This is distinct
from upstream server verification: the debug proxy uses `ssl_insecure=true` and no per-firmware
upstream root bundle. The [network contract](network.md) describes the three network modes;
the per-firmware upstream root bundles were removed.

## Evidence and distribution

Identity files and writable state are not redistributable build outputs. The
[distribution rules](../guides/build.md#what-may-be-distributed) distinguish reusable artifacts
from installation-specific material.

## Development VMs

Checkout launches use managed development libraries in the user's data directory. Development VMs generate their own
identity; different instances have separate identity and state directories.
See the [development guide](../guides/build.md#development-vm-data) for commands.
