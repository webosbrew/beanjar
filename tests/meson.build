# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# The test tiers (docs/dev/guides/testing.md), run by pytest: tests/unit (no build, no firmware),
# tests/integration (this build's outputs, named in artefacts.json). One Meson test per tier and
# area (`meson test -C build/host --suite unit`, `--suite vnc`), plus the compiled checks, which
# Meson builds and runs itself. tests/regress is the VM boot suite (scripts/regress), not run here.
# A run whose every test skipped exits 77 (tests/conftest.py).
xvfb_run = find_program('xvfb-run', required: false)   # the GTK and SDL checks' private X display
subdir('unit')          # the compiled unit checks
subdir('integration')   # the compiled check-audio-context and check-media-sandbox

# What the integration tests run, by name: the `artefact` fixture (tests/conftest.py). A name with
# an empty path is not built here; its tests skip.
test_artefacts = {
  'publish_dir': publish_dir,
  'gtk_python': gtk_python.full_path(),
  'xvfb_run': xvfb_run.found() ? xvfb_run.full_path() : '',
  'viewer_core': viewer_core.full_path(),
  'viewer_headless': viewer_headless.full_path(),
  'media_helper': media_helper.full_path(),
  'relay': relay.full_path(),
  'check_audio_context': check_audio_context.full_path(),
  'check_media_sandbox': check_media_sandbox.full_path(),
  'vnc': have_vnc ? vnc.full_path() : '',
  'manager_gresource': have_gtk ? manager_gresource.full_path() : '',
  'viewer_gresource': have_gtk ? viewer_gresource.full_path() : '',
  'qemu': is_variable('qemu_stage') ? qemu_stage / 'bin' / 'qemu-system-aarch64' : '',
  'qemu_display_xml': is_variable('qemu_stage') ? sources['qemu']['dir'] / 'ui' / 'dbus-display1.xml' : '',
}
artefacts_json = configure_file(output: 'artefacts.json', configuration: test_artefacts,
  output_format: 'json')
pytest = ['-m', 'pytest', '-q', '--no-header']
pytest_env = {'PYTHONDONTWRITEBYTECODE': '1', 'BEANJAR_TEST_MESON': '1',
              'BEANJAR_TEST_ARTEFACTS': meson.current_build_dir() / 'artefacts.json'}
tests_dir = meson.current_source_dir()

# area: seconds
# An area whose directory is not in the tree (a source tree without the maintainers' research tools
# has no model-config or firmware tests) is skipped.
foreach area, timeout : {
    'manager': 600, 'launch': 300, 'regress': 120, 'packaging': 180, 'securestore': 300,
    'netstub': 120, 'web': 60, 'model-config': 60, 'firmware': 60, 'formatting': 60,
    'publish': 60, 'kernel': 120, 'gl': 120, 'ui': 30, 'tcg': 30,
    'layers': 120, 'build': 60,
  }
  if fs.is_dir(tests_dir / 'unit' / area)
    test('unit-' + area, python, args: pytest + [tests_dir / 'unit' / area],
      env: pytest_env, timeout: timeout, suite: ['unit', area])
  endif
endforeach

# area: [seconds, what its tests run]
integration_depends = {
  'network': [180, [relay]],
  'ui': [300, have_gtk ? [manager_gresource] : []],
  'media-helper': [180, [media_helper, check_media_sandbox]],
  'vnc': [600, have_vnc ? [vnc, viewer_headless, media_helper] : []],
  'web': [300, have_vnc ? [vnc] : []],
  'viewer': [600, [viewer_core, viewer_headless, check_audio_context] +
                  (have_gtk ? [viewer_gresource] : []) + (have_vnc ? [vnc] : []) +
                  (is_variable('qemu_stage') ? [qemu] : [])],
  'elements': [600, []],
  'packaging': [120, []],
  'securestore': [120, []],
  'launch': [300, []],             # the real launcher, with the guest build installed
  'bridge': [600, []],             # the qemu-user compat bridge on build/Image under full TCG
}
foreach area, spec : integration_depends
  if fs.is_dir(tests_dir / 'integration' / area)
    test('integration-' + area, python, args: pytest + [tests_dir / 'integration' / area],
      env: pytest_env + {'RELAY': relay.full_path(), 'BEANJAR_RELAY': relay.full_path(),
                         'MEDIAHUB': media_helper.full_path()},
      depends: spec[1], timeout: spec[0], suite: ['integration', area])
  endif
endforeach
# mitm_addon.py's tests need mitmproxy, the user's own: run-mitm-addon.py runs them with the Python
# of the mitmweb mitmtool.py finds (a pipx venv's), as the test starts, or skips (77) saying why.
test('integration-mitm-addon', python,
  args: [tests_dir / 'integration/network/run-mitm-addon.py'],
  env: pytest_env, timeout: 300, suite: ['integration', 'network'])
