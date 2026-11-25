# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The hbchannel layer, one per Homebrew Channel release: Homebrew Channel as an elevated developer app
# (LAYERS=...,hbchannel or hbchannel@<version>; not with rootssh). Made by build-layer.sh from rootfs/
# and an HBC release, versions/<version>: its pinned IPK (release.env), which build-layer.sh downloads
# into Meson's package cache and checks, and the files a real install of it wrote on the webOS
# release, captured/<name>, made by capture.sh. versions/<version>/releases names each webOS
# release's capture: releases whose installs wrote the same files share one, named after the first
# of them. Every release of hbchannel_versions is built, as layers['hbchannel@<version>'], published
# as build/components/hbchannel/versions/<version> (meson/components.py); versions/default names
# the one plain hbchannel means (host/core/beanjar/layermeta.py). The layer holds no key: the
# host key and root's authorized_keys are each VM's own, brought in through the per-VM layer.
hbchannel_versions = ['0.7.3', '0.8.0']   # the shipped releases, each a directory of versions/
hbchannel_default = fs.read('versions/default').strip()
if hbchannel_default not in hbchannel_versions
  error('guest/layers/hbchannel: versions/default names ' + hbchannel_default + ', not one of hbchannel_versions')
endif
layer_defaults += {'hbchannel': hbchannel_default}
foreach hbchannel_version : hbchannel_versions
  hbchannel_capture = ''
  foreach line : fs.read('versions' / hbchannel_version / 'releases').strip().split('\n')
    words = line.strip().split()
    if words.length() == 2 and words[0] == webos
      hbchannel_capture = meson.current_source_dir() / 'versions' / hbchannel_version / 'captured' / words[1]
    endif
  endforeach
  if hbchannel_capture == ''
    error('guest/layers/hbchannel: versions/' + hbchannel_version + '/releases names no capture for ' + webos)
  endif
  hbchannel_name = 'hbchannel@' + hbchannel_version
  layer_meta += {hbchannel_name: meson.current_source_dir() / 'layer.toml'}
  layers += {hbchannel_name: custom_target('hbchannel-layer-' + hbchannel_version,
    output: 'hbchannel-' + hbchannel_version + '-layer.stamp',
    depfile: 'hbchannel-' + hbchannel_version + '-layer.d',
    command: [python, layer_script_py, '--dest', layers_dir / hbchannel_name, '--stamp', '@OUTPUT@', '--depfile', '@DEPFILE@',
      '--dep', meson.current_source_dir() / 'build-layer.sh',
      '--dep', meson.current_source_dir() / 'versions' / hbchannel_version / 'release.env',
      '--dep', meson.current_source_dir() / 'versions' / hbchannel_version / 'releases',
      '--dep-tree', meson.current_source_dir() / 'rootfs',
      '--dep-tree', hbchannel_capture,
      '--', 'env', 'HBC=' + hbchannel_version, 'CAPTURE=' + hbchannel_capture,
      meson.current_source_dir() / 'build-layer.sh'],
    build_by_default: true)}
endforeach
