# Configure secure-store items

Each webOS VM can supply secure-store values through `identity/sestore.toml`.
Names are the exact strings passed to `SESTORE_ReadData`, including regional suffixes. Edit
while the VM is off.
There is no version header: the host validates the TOML structure directly.

```toml
missing = "fallback"

[items]
# Dummy model secret: base64 text for a 32-byte all-zero AES key.
"1D492999" = { text = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=" }

# Dummy SDP common seed: 16 raw bytes.
"3382732D" = { hex = "00000000000000000000000000000000" }

# A generated PEM file beside this TOML.
"617803D8" = { file = "dummy-public.pem" }

# Manually authored YouTube credentials include an explicit trailing NUL.
"C1FFB834" = { text = "dummy-id;dummy-secret", nul_terminated = true }
```

An editor can validate and complete the file with [`sestore.schema.json`](sestore.schema.json),
for example in taplo or Even Better TOML. Put `#:schema ./sestore.schema.json` (a path or URL to
the schema) as the first line of `sestore.toml`, or map the file to the schema with `[[rule]]`
`include` and `[rule.schema] path` in `.taplo.toml`. The schema covers the structure and the
fixed formats of the known items; the host still checks byte sizes, PEM contents and files.

The root `missing` property accepts `fallback` (the default) and `error`. Under either, a sestore
read of an item the TOML does not configure returns `0xFFFF0002` (no such item); LG's own library
is never asked, since it cannot read its store in a VM. Fallback adds the built-in synthetic model
secret as `1D492999` when no model item of any name is configured, and generates the SSG and DVR
keys at launch. Error adds nothing: without a model item the HAL/DILE model-key read fails.
Legacy files are migration inputs only; removing an entry never revives an archived key.

Each entry contains exactly one of `text`, `hex`, or `file`. Text is UTF-8, hex is decoded binary
(two digits per byte, either case, no `0x`; ASCII whitespace between bytes is ignored),
and a file contributes exact bytes. Relative paths resolve beside the TOML; absolute paths are
also accepted. No newline, base64 decoding, trimming or terminator is implicit.
`nul_terminated = true` appends one zero byte to any source. Empty generic items are supported.
Names contain ASCII letters, digits, underscores and hyphens, start with a letter or digit, and
are at most 64 characters. Each decoded value is limited to 65536 bytes; snapshots hold at
most 256 items. The TOML document may be up to 64 MiB so a complete hex export fits in
one file. Names are never hashed, renamed or uppercased.

Keep the TOML and referenced files at mode `0600` inside the VM's `0700` identity directory.
Legacy files must have owner-only permissions when importing them.
Malformed TOML, unknown fields, multiple sources, unreadable files and invalid known formats
stop launch. Diagnostics identify an item and reason without printing its value.

| Item | Required bytes |
| --- | --- |
| `1D492999`, its `_a`, `_d`, `_r` variants, legacy `SDP_MODEL` | Exactly 44 canonical base64 characters encoding 32 bytes; no NUL or newline. |
| `77D1DD90` | A usable private PEM key, at most 1999 bytes, without NUL. Launch generates its matching gateway certificate. |
| `C1FFB834` | Nonempty printable ASCII `<part1>;<part2>` followed by one NUL or LF, at most 100 bytes including that terminator. No embedded control characters. |
| `dvr_key` | Exactly 16 bytes: the DVR's secure key, which MStar's board HAL reads through KADP for the recording middleware (M2, M3 and LM21: W16R, W18R, W21A and their kin). Launch makes a random one when it is missing (not with `missing = "error"`); clone keeps it, `identity reset` makes a new one. |

The observed native OLED C2 YouTube record ends in LF. Preserve that byte in an exact `hex`
or `file` export; the stock DILE consumer replaces the final stored byte with NUL in its
output. TOML loading and guest delivery retain the original bytes. For manually authored
`text` credentials, use `nul_terminated = true` as above. Do not append NUL to an LF-ended
native export, since that would leave an embedded LF.

Regional model items are distinct, as on a TV. From kisscurl (W21A, W21P) on, the DILE and nyx
readers ask for the board's regional item first (`_a` ATSC, `_d` DVB, `_r` ARIB, from LG's
`libdile_tvregion` and the board's `hwopt`), then plain `1D492999`; nyxCrypto reads nothing for a
board whose region is neither. Older lines (W20L, W19P, W18R and before) read only the plain
item, or legacy `SDP_MODEL`. A configured name is answered exactly and an absent one is missing:
an ARIB board with only `1D492999_d` has no model key. Dummy keys exercise local code paths;
external services may reject them.

## Legacy key files

Launch and model-key commands use only TOML: the legacy `model-secret`, `ssg-key` and `youtube-key` files of
an identity are not read. `migrate-local` imports them into `sestore.toml` as part of development migration
(`model-secret` as `1D492999` text, `ssg-key` as `77D1DD90` PEM text, `youtube-key` as `C1FFB834` text with
`nul_terminated = true`), keeping the originals.

`beanjar key model set NAME FILE` and the GUI model-key setting update TOML. Installing replaces
every configured model item with one: the item of the VM's board broadcast where the firmware reads
regional items (`1D492999_a`, `_d` or `_r`), else plain `1D492999`. To move it, change the board
(`beanjar config set NAME broadcast=...`) and install again. `clear` removes all model items,
retaining the policy and other items. CLI and GUI detect TOML-only model keys. Generated SSG keys are written directly into TOML. SSH keys, `tee-huk`
and the proxy CA keep their existing storage.

## Guest delivery

The host helper resolves TOML and synthetic defaults into one little-endian
snapshot, `etc/emulator/sestore.bin`, in the per-VM layer at mode `0600`. TOML and referenced
paths remain on the host. The standalone compiler uses the same implementation:

```sh
PYTHONPATH=host/core python3 -m beanjar.sestore IDENTITY_DIR OUTPUT.bin
```

The initramfs loads the snapshot into `lgemu` through `/dev/lg/beanjar-sestore` before webOS
starts. Missing or invalid provisioning stops boot. The driver publishes a complete validated
snapshot atomically; loading requires `CAP_SYS_ADMIN`, succeeds once per module load, and rejects
subsequent replacements. It clears item bytes at module unload. Reader access is through the
read-only device, mode `0444`, and exposes the VM's configured items.

libsestore and HAL/DILE are device clients. They never read guest key files or parse TOML.
libsestore retains NULL-buffer size queries and its output length ABI; HAL/DILE retain capacity
checks, except the regional DILE read, which passes the caller's buffer to `SESTORE_ReadData` as
LG's does. A missing item is missing. A missing, unreadable or unprovisioned
device fails the read; it cannot silently enable fallback. The host helper resolves synthetic
model identity using the existing fixed value, preserving NDUID behavior.

SSG's matching public certificate still enters the gateway's certificate directory. Its private
key and YouTube credentials are supplied through the store, without compatibility key files.

Native media jails that mount `/dev/lg` see the device directly. Some older browser jails,
including WT1H's `native_builtin`, have a fixed device list; the existing jail hook exposes one
matching character-device node there. It copies no key bytes or policy. The hook removes the
previous Cobalt key/policy copies when an existing jail is reused. Both access paths are tested
with ARM32 clients on the arm64 kernel, after chroot and a drop to an unprivileged UID.

The fixed-width ABI is in `include/webos-sestore.h`: a bundle has an 8-byte policy/count header,
then 72-byte item records and exact payload bytes. Limits are 256 items and 65536 bytes per value.
Ioctl records have the same layout on ARM32 and arm64. The kernel checks names, duplicate records,
lengths, padding and complete bundle consumption before publication.

Generic entries work on webOS targets with the libsestore shim. Older HAL/DILE model paths use
the shared reader; this device does not implement older hardware-only secure-store APIs or TEE
operations. Non-webOS VMs ignore the configuration.

The genuine kernel test runs an isolated, firmware-free QEMU guest with no network or VM state:

```sh
python3 tests/integration/securestore/check-sestore-vm.py --build build/guest-o22-queue
python3 tests/integration/securestore/check-sestore-vm.py --build build/guest-h13-afro
```
