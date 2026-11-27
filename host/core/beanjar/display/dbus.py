# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""A VM's private D-Bus (display = "dbus"): the core's one module that talks D-Bus, through Gio
(python3-gi). It imports GLib and Gio, never GTK, and only inside a call, so the CLI runs without
PyGObject until it touches a VM's bus (tests/unit/manager/test_core_no_gtk.py).

  Bus(path, needs, fails)      a connection to the bus at its socket path (core.VM.dbus_sock);
                               call() and close()
  display_size(bus)            QEMU's console 0: the guest display's (width, height)
  screenshot(path, ...)        org.beanjar.Display1.Screenshot of the front end that owns the display
  set_ui_info(address, ...)    SetUIInfo on QEMU's console 1, the stock secondary display
                               (secondary.py)

Errors are base.Error with the remote error's name stripped from the message (CallError for a
call, which also carries that name), worded by the caller."""

from ..base import Error

SCREENSHOT_NAME = "org.beanjar.Display1"  # on the VM's bus: the viewer's or beanjar-vnc's (core.c)
SCREENSHOT_PATH = "/org/beanjar/Display1"


def gio(what=None):
    """(Gio, GLib). Without python3-gi: ImportError, or with what, Error "<what> needs Python Gio
    (python3-gi)"."""
    try:
        from gi.repository import Gio, GLib
    except ImportError:
        if what is None:
            raise
        raise Error(f"{what} needs Python Gio (python3-gi)") from None
    return Gio, GLib


class CallError(Error):
    """A failed call: message without the remote error's name, remote that name (or None)."""

    def __init__(self, message, remote=None):
        super().__init__(message)
        self.message, self.remote = message, remote


class Bus:
    """A connection to the bus whose socket is at path (a message bus, as the VM's dbus-daemon).
    Its errors: "<needs> needs Python Gio (python3-gi)", "<fails>: <the connection's error>"."""

    def __init__(self, path, needs="the VM's bus", fails=None):
        self.Gio, self.GLib = Gio, GLib = gio(needs)
        try:
            self.conn = Gio.DBusConnection.new_for_address_sync(
                f"unix:path={path}",
                Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
                | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
                None,
                None,
            )
        except GLib.Error as e:
            Gio.DBusError.strip_remote_error(e)
            raise Error(f"{fails or needs}: {e.message}") from None

    def call(self, dest, path, iface, method, signature=None, values=(), reply=None, timeout_ms=-1):
        """iface.method of dest at path with values (GVariant signature, e.g. "(ub)"; None: no
        arguments), its reply unpacked if reply names its type (e.g. "(b)"), else None.
        CallError on failure."""
        Gio, GLib = self.Gio, self.GLib
        try:
            result = self.conn.call_sync(
                dest,
                path,
                iface,
                method,
                GLib.Variant(signature, values) if signature else None,
                GLib.VariantType(reply) if reply else None,
                Gio.DBusCallFlags.NONE,
                timeout_ms,
                None,
            )
        except GLib.Error as e:
            remote = Gio.DBusError.get_remote_error(e)
            Gio.DBusError.strip_remote_error(e)
            raise CallError(e.message, remote) from None
        return result.unpack() if reply else None

    def name_has_owner(self, name, timeout_ms=-1):
        return self.call(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "NameHasOwner",
            "(s)",
            (name,),
            "(b)",
            timeout_ms,
        )[0]

    def close(self):
        self.conn.close_sync(None)


def display_size(bus, timeout_ms=5000):
    """(width, height) of the guest's display, QEMU's console (org.qemu.Display1.Console Width and
    Height) on the VM's bus (a Bus)."""
    size = []
    for prop in ("Width", "Height"):
        try:
            reply = bus.call(
                "org.qemu",
                "/org/qemu/Display1/Console_0",
                "org.freedesktop.DBus.Properties",
                "Get",
                "(ss)",
                ("org.qemu.Display1.Console", prop),
                "(v)",
                timeout_ms,
            )
        except CallError as e:
            raise Error(f"QEMU's display: {e.message}") from None
        size.append(int(reply[0]))
    if size[0] < 1 or size[1] < 1:
        raise Error("QEMU's display has no size yet")
    return tuple(size)


def screenshot(path, ppm, width, height, timeout):
    """Calls Screenshot on SCREENSHOT_NAME on the VM's bus (its socket path): True when the front
    end has written the PPM, False if the name has no owner."""
    ms = int(timeout * 1000)
    what = "the display's front end"
    bus = Bus(path, "a screenshot", what)
    try:
        if not bus.name_has_owner(SCREENSHOT_NAME, ms):
            return False
        bus.call(
            SCREENSHOT_NAME,
            SCREENSHOT_PATH,
            SCREENSHOT_NAME,
            "Screenshot",
            "(suu)",
            (ppm, width, height),
            None,
            ms,
        )
    except CallError as e:
        raise Error(f"{what}: {e.message}") from None
    finally:
        bus.close()
    return True


def set_ui_info(address, timeout):
    """SetUIInfo on QEMU's console 1 (the stock secondary display) at the D-Bus address, within
    timeout seconds: 200x500 mm and 768x2160. Errors are Gio's own (GLib.Error), and
    ImportError without python3-gi (display_setup.prepare words both)."""
    import threading

    Gio, GLib = gio()
    cancel = Gio.Cancellable()
    timer = threading.Timer(timeout, cancel.cancel)
    timer.start()
    connection = None
    try:
        connection = Gio.DBusConnection.new_for_address_sync(
            address,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None,
            cancel,
        )
        connection.call_sync(
            "org.qemu",
            "/org/qemu/Display1/Console_1",
            "org.qemu.Display1.Console",
            "SetUIInfo",
            GLib.Variant("(qqiiuu)", (200, 500, 0, 0, 768, 2160)),
            None,
            Gio.DBusCallFlags.NONE,
            max(1, int(timeout * 1000)),
            cancel,
        )
    finally:
        timer.cancel()
        timer.join()
        if connection is not None:
            connection.close_sync(None)
