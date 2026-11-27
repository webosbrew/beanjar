# host/helpers/rtk

Host-side helpers for the Realtek SoCs (k5lp W19P, k6lp W20L, k8 W22H) and `rtk.ko`
(guest/kernel/modules/rtk).

## rpc-table.py: rtk.ko's RPC answers from libkadaptor

rtk.ko answers every KADP RPC with 4 bytes of S_OK. The exceptions are module parameters:

| Parameter | Syntax (rtk-rpc.c) | Array size | What |
|---|---|---|---|
| `rpc_reply` | `P:N:BYTES,...` | 128 | result sizes other than 4 |
| `rpc_create` | `P:N,...` | 16 | Creates: `{S_OK, new id}`, 8 bytes |
| `rpc_answer` | `P:N:WORD:VALUE:RESULT,...` | 16 | first word RESULT when argument word WORD is VALUE |

They differ per firmware (W19P's `AIN_GetAudioFormatInfo` result is 20 bytes, W20L's 24), so they
are generated from the firmware's own `libkadaptor.so.1` and never committed.

    host/helpers/rtk/rpc-table.py /srv/beanjar/firmware/05.40.97.01-HE_DTV_W19P_AFADABAA --cache
    host/helpers/rtk/rpc-table.py path/to/libkadaptor.so.1.0.0 --json table.json
    host/helpers/rtk/rpc-table.py --self-test [--cache] [--firmwares DIR]

The source is a firmware directory, where `bsppart*.pak`'s `bsp/usr/lib` (or `rootfs/usr/lib`) is
unpacked into a temporary directory and removed afterwards, or a library file. The script prints
the parameters on stdout (one line, space-separated words) and a summary on stderr. `--cache`
writes `build/rtk/<firmware>/rpc-table.json` and `params` and reuses them while the library's
sha256 is the same. `--self-test` runs on W19P, W20L and W22H and checks that each gives the
Creates `100:10,201:1` and the AudioConfig answer.

It needs `python3-pyelftools`, `python3-capstone` and `unsquashfs`.

### How it reads the library

- The stubs are every caller of `client_call` (the `.text` is scanned for Thumb and ARM BL/BLX to
  it or its PLT entry). They are the `*_ToAgent_*_0` functions, plus `AUDIO_RPC_ENC_SetEncoder_0`,
  `VIDEO_RPC_DOLBY_VISION_HDMI_*_0` and the debug program's `set_debug_flag_0`. No other ELF in
  the three firmwares calls `client_call` (librpccore and libracodec import the stubs).
- For each stub, a register tracker over its disassembly gives the `malloc` size (the result
  buffer: the reply size), `r1` at `client_call` (the procedure), `r2` at `rtk_xdrmem_create`
  (the XDR size, checked against the malloc size) and the decoder after it.
- The program is not in the stub: `prepareRPCCall` copies it from the caller's clnt. It comes from
  the name (AUDIO → 201, VIDEO → 100) and is checked against every call site in libkadaptor whose
  clnt is a `prepareCLNT(clnt, mode, program, 0)` on its stack. The same pass counts the sites
  whose mode has BLOCK_MODE (bit 0). A stub mallocs its result only when the clnt is BLOCK_MODE,
  so blocking is a property of the call site, not the stub.
- A Create is a `*_Create_0` that decodes `xdr_RPCRES_LONG` into 8 bytes. The other 8-byte
  results (`GetStatus`, `RequestBuffer`, ...) go into `rpc_reply`.
- `rpc_answer=201:16:0:113:0x10000003` (AudioConfig, `AUDIO_CONFIG_CMD_GET_TASK_STARTED`) is
  emitted with AudioConfig's procedure. It is verified where a library next to libkadaptor holds a
  function with both constants (W19P's `RHAL_AUDIO_InitializeModule`). Elsewhere it is reported as
  unverified.

It exits non-zero on anything it cannot resolve: a procedure or malloc size that is not one
constant, two sizes for one (program, procedure), a prefix that disagrees with the callers, a
program-less stub whose result is not 4 bytes, or more entries than rtk.ko's arrays hold.

## Wiring into the launcher

A chip whose `SocModule` has `derived="rpc-table"` (k5lp: `rtk`) gets these parameters at every
start: the launcher (`beanjar/runtime/launch.py` through `targets.derived_params`) runs
`rpc-table.py <firmware dir> --cache --name <firmware name>` and appends its stdout words to the
module's table parameters in boot.conf (`MODULE_PARAMS_rtk`), which guest/initramfs/rootfs/init passes to
`insmod /lib/modules/rtk.ko`. The words have no white space or quotes, as `_words()` needs.

`--cache` keeps `build/rtk/<firmware>/`; a cache hit needs neither capstone nor pyelftools (they
are imported only to analyse), so a host that only has `unsquashfs` for the sha256 check can use a
`build/rtk` copied from another.

Length: about 330 bytes on all three firmwares (23 `rpc_reply`, 2 `rpc_create`, 1 `rpc_answer`).
That is not near any limit on this path. boot.conf is sourced from the host share, not the kernel
command line. Busybox insmod joins its arguments with no fixed limit, one argument may be up to
MAX_ARG_STRLEN (128 KiB), and init_module copies the whole string. The limits that matter are
rtk.ko's array sizes (128/16/16), and the script checks them. If the table ever outgrows them,
raise the array sizes in rtk-rpc.c, or have init pass a params file from /host instead of arguments.
