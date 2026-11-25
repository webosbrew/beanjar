# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The soc layer, build/layers/<chip>-<release>/soc, always used: what the emulator stands in for below
# the LG userland on the build's SoC. One source
# tree per shim for every chip (src/), compiled with the chip's table (guest/layers/soc/<chip>/meson.build:
# hal_stock, gst_plugin_dir), plus the chip's own files (soc_layer_trees: guest/layers/soc/<chip>/rootfs,
# after its base's where it names one, docs/dev/guides/build.md "Layer bases").
#
# The shims of LG's HAL and DILE libraries are system libraries, loaded by every process that links
# the stock one (tvservice, tvwin, gm, libgm's and libim's clients, libsecretagent's); what is
# private to one process is elsewhere (tvservice's view of the OS: guest/layers/common/tvservice).
# Each shim replaces the stock symlink under its soname and NEEDs the stock file by its full name
# (unchanged): the dynamic linker loads the shim for every "<soname>" and the stock file right
# after it, so the functions a shim defines resolve there and every other symbol to LG's code.
# The stock file's SONAME is the soname itself, so linking it directly would record that name; an
# empty link-time stand-in carries the file name as its SONAME instead
# (guest/layers/common/hal-common/link-stub.c). One subdirectory of src/ per library:
#   libhal_lg115x/   audio, video scaler, analog decoder, section decoder
#   libdile_vt/      tv.capture's video plane (DILE_VT's older interface: H13)
#   libdile_gal/     DILE_GAL on the emulator's graphics memory (the SoC module's /dev/gfx)
#   libhal_crypto/   the secure store's model secret (NDUID)
#   libdile_crypto/  the model secret's DILE read, as stock does it after its initialisation
#                    (dile-crypto.c), and on M3 and K5LP the debug status's pass-through cipher (dbg-data.c)
#   libsestore/      secure store items by their exact names: the second screen gateway's TLS key
#                    (every firmware whose gateway reads it), the model secret (DILE's regional read,
#                    and nyx crypto's from queue on)
#   libhal_avd/      the analog decoder's no signal over MStar's board HAL (hal-avd.c)
#   libkadaptor/     M2's XC window calls, passed to the kernel too (kadaptor-xc.c); O22's
#                    Widevine OEMCrypto without a TEE (kadaptor-oemcrypto.c) and secure clock
#                    through nyxDrm without a PlayReady TA (kadaptor-securetime.c); M2's and M3's
#                    DVR key from the VM's store (kadaptor-pvrkey.c)
#   libhal_libs/     LM21's board HAL: the DVR key from the VM's store (hal-pvrkey.c)
#   libaibsp/        O22's AI picture BSP for nn-proxy, with no NPU (every entry a no-op)
#   libaibspLib/     O22's AI tone and scene services for pqcontroller, likewise
#   libhal_direct/   the direct video decoder and audio output under DILE's direct libraries
#                    (NDL DirectMedia's first API), to the host's media ports
#   omx_core/        MStar's OpenMAX IL core under NDL's Esplayer (W15U), to the host's media
#                    ports
#   video/           the media elements under their stock names, in gst_plugin_dir (none if '')
#
# The board's I2C devices (the micom, the NVM EEPROM, the amplifier) are not here: they are in
# the kernel (lgemu.ko, on lg115x.ko's and mstar-utopia.ko's buses), under the stock HAL_I2C_*.

# Choosing the shims (docs/dev/architecture/build-and-boot.md, "Choosing the shims"): by what the
# firmware's rootfs image holds (fw_path, guest/targets/meson.build), not by a release's name, so a
# release newer than the last one studied gets the shims its consumers need.
#
# libsestore: wherever the stock library is there and something reads secure store items through it
# directly (src/libsestore/sestore.c): the second screen gateway, for its TLS key
# (helpers/bus/ssl-cert-getter.js, webOS 5 on), or nyx's secure store module, which nyx crypto
# reads the model secret through (nyxSecureStore.module NEEDs libsestore.so.0: queue and spacebuns),
# and the libdile_crypto shim's regional read goes through it too.
sestore_nyx = 'usr/lib/nyx/modules/nyxSecureStore.module'
ssg_getter = 'usr/palm/services/com.webos.service.secondscreen.gateway/helpers/bus/ssl-cert-getter.js'
sestore_stock = 'usr/lib/libsestore.so.0.1.0'
fw_questions += ['path ' + sestore_nyx, 'path ' + ssg_getter, 'path ' + sestore_stock]
fw_sestore_nyx = fw_path.get(sestore_nyx, false)
if fw_path.get(sestore_stock, false) and (fw_path.get(ssg_getter, false) or fw_sestore_nyx)
  hal_stock += {'libsestore': ['libsestore.so.0', 'libsestore.so.0.1.0', []]}
endif

# The Qt EGLFS Starfish setup (eglfs-starfish/): wherever the firmware's compositor is Qt with LG's
# eglfs_starfish integration, whatever the chip: the rootfs ships the plugin, product.env exports
# QT_QPA_EGLFS_INTEGRATION=eglfs_starfish, and no Weston reads them (weston.ini: the queue firmwares
# keep plugin and env file beside a Weston compositor). W23O's, W24G's, W24O's, W25G's and W25O's
# ombre and ponytail, W23A's, W25H's and W25P's ponytail; not W22O's, W22H's, W23H's, W23M's, W23P's,
# W24H's or W24P's ombre and ponytail, which export an empty integration (the legacy starfish QPA
# through the gl layer's libegl-kms.so) and have no plugin. The plugin reads the planes' IN_FORMATS
# blob without a check: the tree's usr/share/beanjar/kernel.cmdline gives the launcher
# virtio_gpu.modifiers=1 (docs/research/k24n-starfish-in-formats.md).
starfish_plugin = 'usr/lib/plugins/egldeviceintegrations/libeglfs-starfish-integration.so' # usr/lib/qt5 links to usr/lib
starfish_env = 'etc/surface-manager.d/product.env:QT_QPA_EGLFS_INTEGRATION=eglfs_starfish'
weston_ini = 'usr/share/weston/weston.ini'
fw_questions += 'path ' + starfish_plugin
if fw_path.get(starfish_plugin, false)
  fw_questions += 'path ' + weston_ini
  if not fw_path.get(weston_ini, false)
    fw_questions += 'exports ' + starfish_env
    if fw_exports.get(starfish_env, false)
      soc_layer_trees += ['--tree', meson.current_source_dir() / 'eglfs-starfish', '.']
    endif
  endif
endif

# A chip's table names the shims its stock libraries may need; one whose stock file the rootfs does
# not have is not built (a 'bsppart' one is bsppart's, not listed here).
#
# libhalgal serves HAL_GAL from the software GAL engine (src/libdile_gal). With the stock libdile_gal
# in the rootfs it uses the libdile_gal shim's engine, shared with the DILE callers. Without one, it
# carries the engine itself only where the stock HAL needs hardware the emulator does not have:
# where bsppart's libkadaptor, which the stock libhalgal NEEDs, defines RHAL_SE_Init, Realtek's
# SE 2D engine. Its KADP_GAL_Init runs RHAL_SE_Init, whose se_open maps /dev/se0 and asserts
# (se_drv.c:167) when it cannot; rtk.ko's se0 has no mmap. A chip whose stock HAL cannot allocate
# a surface on the emulator at all says so in its table (halgal_stock_fails: LG's O22 family, MStar's LM21), and
# gets the engine without the question. Any other firmware whose libkadaptor has no SE engine keeps
# the stock HAL.
halgal_se = 'bsppart:bsp/usr/lib/libkadaptor.so:RHAL_SE_Init'
halgal_stock_fails = get_variable('halgal_stock_fails', false)
halgal_own_engine = false
shims = {}
foreach name, sonames : hal_stock    # [soname, stock file, aliases(, 'bsppart': not in the rootfs)]
  stock = 'usr/lib/' + sonames[1]
  gal_stock = 'libdile_gal' in hal_stock ? 'usr/lib/' + hal_stock['libdile_gal'][1] : ''
  if sonames.length() < 4
    fw_questions += 'path ' + stock
  endif
  if name == 'libhalgal' and gal_stock != ''
    fw_questions += 'path ' + gal_stock
  endif
  if name == 'libhalgal' and not fw_path.get(gal_stock, false) and not halgal_stock_fails
    fw_questions += 'defines ' + halgal_se
  endif
  if sonames.length() < 4 and not fw_path.get(stock, false)
    message('soc ' + soc + ': no ' + stock + ' in the firmware, no ' + name + ' shim')
  elif name == 'libhalgal' and not fw_path.get(gal_stock, false)
    if halgal_stock_fails
      message('soc ' + soc + ': no libdile_gal, the stock HAL cannot allocate here: libhalgal carries the software engine')
      halgal_own_engine = true
      shims += {name: sonames}
    elif fw_defines.get(halgal_se, false)
      message('soc ' + soc + ': no libdile_gal, the stock HAL reaches the SE engine: libhalgal carries the software engine')
      halgal_own_engine = true
      shims += {name: sonames}
    else
      message('soc ' + soc + ': no libdile_gal, the stock HAL needs no SE engine: no libhalgal shim')
    endif
  else
    shims += {name: sonames}
  endif
endforeach
hal_stock = shims

hal_libs = {}                      # path under usr/lib -> shim
hal_libs_stock = {}                # path under usr/lib -> the stock file it NEEDs
hal_files = {}                     # path in the layer -> a shim's other file (its jobs)
foreach name : ['libhal_lg115x', 'libdile_vt', 'libdile_gal', 'libhalgal', 'libhal_crypto', 'libdile_crypto', 'libsestore', 'libhal_avd', 'libkadaptor', 'libhal_libs',
    'libaibsp', 'libaibspLib']
  if name in hal_stock
    subdir('src' / name)
  endif
endforeach
video_layer_args = []
if gst_plugin_dir != ''            # a chip without media elements has ''
  subdir('src/video')              # the chip's gst_elements
endif
if gst_plugin_dir != '' and ('libhal_vdec_direct' in hal_stock or 'libhal_audio_direct' in hal_stock)
  subdir('src/libhal_direct')      # with the media elements' GLib
endif
omx_core = get_variable('omx_core', '')
if gst_plugin_dir != '' and omx_core != ''
  subdir('src/omx_core')           # the chip's OpenMAX IL core, with the media elements' GLib
endif

soc_layer_args = []
foreach name, lib : hal_libs
  soc_layer_args += ['--file', lib, 'usr/lib' / name, '755']
endforeach
foreach path, src : hal_files
  soc_layer_args += ['--file', src, path, '644']
endforeach
foreach name, sonames : hal_stock
  foreach alias : sonames[2]
    soc_layer_args += ['--link', sonames[0], 'usr/lib' / alias]
  endforeach
endforeach
layers += {'soc': custom_target('soc-layer',
  output: 'soc-layer.stamp',
  depfile: 'soc-layer.d',
  command: arm_stage + ['--out', layers_dir / 'soc', '--stamp', '@OUTPUT@', '--depfile', '@DEPFILE@']
    + soc_layer_trees + soc_layer_args + video_layer_args,
  build_by_default: true)}

foreach name, lib : hal_libs
  test('soc-' + name.split('.so')[0] + '-needs-stock', find_program('sh', native: true),
    args: ['-c', '"$1" -d "$2" | grep -q "(NEEDED).*\\[' + hal_libs_stock[name] + '\\]"', 'sh',
      readelf, lib],
    suite: 'layers')
endforeach
