# Partition maps and disks

The VM exposes the partition names and indices that stock firmware expects. The launcher
derives a map from the supplied firmware using `host/core/beanjar/board/partmap.py` and the
target's `PartmapFile` or `PartmapLayout` declaration.

## Map and namespace

The map uses LG's `struct partmap_info` layout. Most targets take the embedded table from the
firmware. Where a supplied table is stale, a target-specific layout preserves the established
names and pinned indices while using the attached images' sizes. This is declared in
the target table (`guest/targets/partmap/`), not guessed by initramfs.

The launcher generates the host cache image and creates the VM's writable partinfo state
disk from it. The emulator's `/proc/partinfo` and `/dev/mmcblk0pN` nodes let stock clients find
the declared partitions. Firmware paks remain separate read-only inputs; writable partitions
are VM state. The boot manifest supplies the attachments and mappings to initramfs.

The eMMC itself is `mmcblk0` (179:0) in `/proc/partitions` and `/sys/block`, as on a set, sized
from the map's device size (`lgemu.ko`'s `emmc.c`): stock clients read its size, such as
libluna-prefs' `storageCapacity`, without which `getAllSysProperties` answers nothing. It has no
partitions of its own, since the partitions are the attached disks behind the `/dev/mmcblk0pN`
nodes; its own blocks read as zeros and it is read-only. `firmware.updatable` replaces the
`/dev/mmcblk0` node with its writable whole-disk device.

## Stock filesystem ownership

Stock services format and mount the writable partitions they own. The emulator supplies blank
state where the target specifies it, including secure-store areas for which it has no provisioned
device data. A successful partition lookup does not imply a keybox or other secure content.

Initramfs mounts the declared root/BSP pak layers and preserves the emulator overlays above
them. Legacy `lginit` mounts the additional paks assigned to it by those releases. The actual
partition sizes, attachment modes and ownership are per-target data; one platform's mount scheme
must not be inferred from another's.

## State and validation

Guest reset and formatting act on the VM's attached state disks. They do not change the supplied
firmware collection. Map encoding/decoding, CRCs, partition nodes and launcher attachments have
host-side tests in `tests/unit/launch/test_partmap.py`. Stock boots establish whether the declared
map satisfies a particular firmware's lookups.
