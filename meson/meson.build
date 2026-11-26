# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# What every module uses: the build's own scripts, and the unpacked source trees of the wraps.
# The Python scripts are run with `python`; what they share (stamps, depfiles, locks, tree hashes)
# is in common.py.
python = find_program('python3')
meson_cmd = find_program('meson')      # the Meson running this build (the system's)
stage_py = files('stage.py')           # assemble a tree (layer, SSH binaries, test programs)
publish_py = files('publish.py')       # copy results to publish_dir without disturbing a VM
initramfs_py = files('initramfs.py')   # pack the initramfs with the kernel's gen_init_cpio
layer_script_py = files('layer-script.py')   # layers made by their own build-layer.sh
kbuild = files('kbuild.sh')            # the kernel's and busybox's own Kbuild
# What a guest build makes once for every target that would make it the same, each in
# <shared_dir>/<component>/<key>: the Mesa stack, the arm64 kernel and its modules, busybox and the
# SSH programs. shared.py makes every key; for all but the Mesa stack (mesa-stack.sh) it also
# takes the lock, skips a build already done and copies the result into the build directory.
shared_py = files('shared.py')
shared_dir = top / (get_option('shared_dir') != '' ? get_option('shared_dir') : 'build')

# subprojects/<name>.wrap, unpacked into subprojects/<directory> by Meson's own wrap handling
# (meson/fetch.py runs `meson subprojects download` and redoes it when the wrap or a patch
# changes). None is configured as a Meson subproject: the trees are built by their own build
# systems from custom targets, which depend on the 'stamp' of the tree they use. A tree is fetched
# only when something that is built needs it.
fetch_py = files('fetch.py')
sources = {}
foreach name : ['linux', 'busybox', 'gstreamer', 'gstreamer18', 'gstreamer112', 'gstreamer1141', 'gstreamer1144', 'gstreamer1162', 'dropbear', 'openssh', 'libffi', 'libdrm',
                'wayland', 'wayland120', 'wayland122', 'wayland-protocols', 'mesa', 'usbipdcpp']
  directory = ''
  foreach line : fs.read(top / 'subprojects' / name + '.wrap').split('\n')
    if line.startswith('directory')
      directory = line.split('=')[1].strip()
    endif
  endforeach
  sources += {name: {
    'dir': top / 'subprojects' / directory,
    'version': directory.split(name + '-')[-1],     # linux-6.18.53 -> 6.18.53
    'stamp': custom_target('fetch-' + name,
      output: name + '.fetched',
      depfile: name + '.fetched.d',
      command: [python, fetch_py, '--source-root', top, '--stamp', '@OUTPUT@', '--depfile', '@DEPFILE@',
                name, '--', meson_cmd]),
  }}
endforeach

if not guest_lane
  subdir('coverage')                     # -Db_coverage=true: docs/dev/guides/coverage.md
  # The GTK 4 windows, the viewer's and the VM manager's (host/ui, beanjar_ui), are Python
  # (PyGObject) with their layouts in Blueprint files (host/ui/data/<window>/*.blp), compiled
  # into a GResource bundle each. GTK 4 and libadwaita for the system Python (python3-gi,
  # gir1.2-gtk-4.0, gir1.2-adw-1) are what they run with, and blueprint-compiler runs with them
  # too: it reads the installed typelibs (through GIRepository) to check every class and property,
  # so a .blp using something newer than this host's GTK or libadwaita does not build. The code
  # needs GTK 4.12 (GtkGLArea.set_allowed_apis, GtkCssProvider.load_from_string, EGL on X11 by
  # default) and libadwaita 1.7 (AdwInlineViewSwitcher); Debian 13 has 4.18 and 1.7. Without
  # them the bundles are not built: the viewer runs headless only, the manager's CLI works and
  # its GUI does not.
  gtk_python = find_program('/usr/bin/python3', 'python3')   # the system Python, with PyGObject
  gtk_check = run_command(gtk_python, '-c',
    'import warnings; warnings.simplefilter("ignore"); ' +
    'import gi; gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1"); ' +
    'from gi.repository import Adw, Gtk; from gi.repository import GIRepository; ' +
    'print("%d.%d.%d %d.%d.%d" % (Gtk.get_major_version(), Gtk.get_minor_version(), ' +
    'Gtk.get_micro_version(), Adw.get_major_version(), Adw.get_minor_version(), Adw.get_micro_version()))',
    check: false)
  gtk_versions = gtk_check.stdout().strip().split(' ')
  have_gtk = (gtk_check.returncode() == 0 and gtk_versions.length() == 2 and
    gtk_versions[0].version_compare('>= 4.12') and gtk_versions[1].version_compare('>= 1.7'))
  if have_gtk
    message('GTK ' + gtk_versions[0] + ' and libadwaita ' + gtk_versions[1] +
            ' through PyGObject: the GTK windows are built')
  else
    message('no GTK >= 4.12 and libadwaita >= 1.7 for Python (python3-gi, gir1.2-gtk-4.0, ' +
            'gir1.2-adw-1): the viewer runs headless only, the manager has no GUI')
  endif
  # Debian 13's blueprint-compiler (0.16; the Python script runs with the system python3).
  blueprint = find_program('blueprint-compiler', version: '>= 0.16', required: have_gtk)
  gnome = import('gnome')

  # The prebuilt third-party programs (meson/vendor.py): QEMU, FFmpeg, the media helper's FFmpeg
  # and neatvnc with aml, as beanjar-vendor releases them for this architecture, pinned in
  # packaging/vendor.toml (reading it here reconfigures when the pin changes). Each is unpacked
  # once into build/vendor and published as it is to publish_dir (build/qemu, build/ffmpeg,
  # build/ffmpeg-helper, build/neatvnc), where the
  # launcher runs QEMU from and the programs linking the libraries find them; the packages ship
  # the same trees (packaging/debian/build.py).
  vendor_arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(host_machine.cpu_family(), '')
  if vendor_arch == ''
    error('beanjar-vendor has no prebuilt programs for ' + host_machine.cpu_family() +
          ' (amd64 and arm64 only)')
  endif
  fs.read(top / 'packaging/vendor.toml')
  vendor_programs = ['qemu', 'ffmpeg', 'ffmpeg-helper', 'neatvnc']
  # qemu-user's qemu-arm (the compat bridge, docs/dev/architecture/qemu-user-bridge.md) runs in the
  # guest: its arm64 tarball, once vendor.toml pins it (or BEANJAR_VENDOR_QEMU_USER names one), is
  # published as build/qemu-user; on an amd64 host also its amd64 one, qemu-user:amd64, for the
  # experimental x86-64 guest kernel, as build/qemu-user-amd64
  vendor_programs += run_command(python, files('vendor.py'), 'pinned', 'qemu-user',
                                 vendor_arch == 'amd64' ? ['qemu-user:amd64'] : [],
                                 check: true).stdout().strip().split()
  vendor_trees = run_command(python, files('vendor.py'), 'tree', vendor_arch, vendor_programs,
                             check: true).stdout().strip().split('\n')
  vendor = {}
  foreach i : range(vendor_programs.length())
    vendor += {vendor_programs[i].replace(':', '-'): vendor_trees[i]}
  endforeach
  # The libraries through their own pkg-config files, which find their tree from where they are
  # (prefix=${pcfiledir}/../..), with this machine's pkg-config (the cross file's in the arm64
  # build) and the set's trees first on its path. neatvnc's Requires.private names the FFmpeg
  # libraries, which resolve to the private copy, and Debian's -dev packages of its other
  # libraries (role host, packaging/debian/build-deps). The trees' own -I come first, and a
  # system directory such as /usr/include/<triplet>, where Debian's libavcodec-dev would have its
  # headers, is dropped: it must never come before the private copy's. Programs linking these need RUNPATH /usr/lib/beanjar/<program>/lib
  # (installed) and $ORIGIN/../<program>/lib (published beside build/<program>): vendor_rpath.
  vendor_pkg_config = find_program('pkg-config')
  # The media helper links its own FFmpeg (ffmpeg-helper: its decoders only, no encoder and no
  # libavformat), looked up the same way from its tree alone: vendor_deps['ffmpeg-helper/<name>'].
  # beanjar-vnc and the rest take the full build's, vendor_deps['<name>'].
  vendor_deps = {}
  foreach set : [['', ['ffmpeg', 'neatvnc'], ['libavcodec', 'libavfilter', 'libavutil',
                   'libswresample', 'libswscale', 'neatvnc', 'aml1']],
                 ['ffmpeg-helper/', ['ffmpeg-helper'], ['libavcodec', 'libavutil',
                   'libswresample']]]
    trees = []
    pc_path = []
    foreach program : set[1]
      trees += vendor[program]
      pc_path += vendor[program] / 'lib/pkgconfig'
    endforeach
    env = environment({'PKG_CONFIG_PATH': ':'.join(pc_path)})
    foreach name : set[2]
      args = {}
      foreach what : ['cflags', 'libs']
        own = []
        other = []
        foreach f : run_command(vendor_pkg_config, '--' + what, name, env: env,
                                check: true).stdout().strip().split()
          is_own = false
          foreach tree : trees
            is_own = is_own or f.startswith('-I' + tree)
          endforeach
          if is_own
            own += f
          # not the compiler's own system directories, which it searches anyway, after every -I
          elif (f not in other and f != '-I/usr/include' and
                not (f.startswith('-I/usr/include/') and f.endswith('-linux-gnu')))
            other += f
          endif
        endforeach
        args += {what: own + other}
      endforeach
      vendor_deps += {set[0] + name: declare_dependency(compile_args: args['cflags'],
        link_args: args['libs'],
        version: run_command(vendor_pkg_config, '--modversion', name, env: env,
                             check: true).stdout().strip())}
    endforeach
  endforeach
  vendor_rpath = {}
  foreach program : ['ffmpeg', 'ffmpeg-helper', 'neatvnc']
    vendor_rpath += {program: '$ORIGIN/../' + program + '/lib:/usr/lib/beanjar/' + program + '/lib'}
  endforeach
  summary({'QEMU': vendor['qemu'], 'FFmpeg': vendor['ffmpeg'],
           'FFmpeg (media helper)': vendor['ffmpeg-helper'], 'neatvnc': vendor['neatvnc']},
          section: 'Prebuilt (packaging/vendor.toml)')
endif
