# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The manager header's state button: one icon-only Adw.SplitButton whose icon, tooltip (the action
it takes, also its accessible label), style, action and dropdown follow the selected VM's state
(gui.Manager.update_actions applies it). Off it starts the VM, running it connects the viewer, with
the viewer open it shuts the VM down; the dropdown has the variants (Start and Connect, Start with
Reason…) or the rarer steps (Shut Down…, Disconnect, Unplug…). Whether each action can run is its own (the window's actions' sensitivity); this only
picks which one the button shows. No GTK here: the tests check the mapping without a display."""

from dataclasses import dataclass

# core's phases (beanjar.base.OFF, ...), as strings: this module imports neither GTK nor the core
OFF, STARTING, RUNNING, STOPPING = "Off", "Starting", "Running", "Stopping"

START_ICON = "media-playback-start-symbolic"
CONNECT_ICON = "video-display-symbolic"
SHUTDOWN_ICON = "system-shutdown-symbolic"
WORKING_ICON = "content-loading-symbolic"

# what the button says while an operation of the selected VM runs (gui.Manager.ops' kinds)
BUSY = {
    "start": ("Starting…", START_ICON),
    "shut down": ("Shutting Down…", SHUTDOWN_ICON),
    "unplug": ("Unplugging…", SHUTDOWN_ICON),
    "wipe": ("Wiping…", WORKING_ICON),
    "new identity": ("Renewing…", WORKING_ICON),
    "disconnect": ("Disconnecting…", CONNECT_ICON),
    "clone": ("Cloning…", WORKING_ICON),
    "install MITM": ("Installing…", WORKING_ICON),
}


@dataclass(frozen=True)
class Slot:
    """The state button: label, the action it names (its accessible label), and icon; action, the
    window action its main part runs (win.<action>; None: the button is insensitive, dropdown
    included); menu, its dropdown (a menu of window.blp); suggested: the suggested-action style;
    tooltip, the label or why the action cannot run. While an operation runs the button is
    insensitive with the state's icon (the header's spinner shows that something runs) and its
    tooltip says what runs."""

    label: str
    icon: str
    action: str | None
    menu: str | None
    suggested: bool
    tooltip: str


def slot(phase=OFF, busy=None, blocked=None, pid=False, viewer=False, display="dbus"):
    """The state button for a VM in phase (core's state; None: no VM, or one whose vm.toml does
    not read), with busy the kind of its running operation (None: none), blocked why it cannot
    start (capacity), pid whether QEMU runs, viewer whether its viewer is open and display its
    display (the viewer needs dbus)."""
    if busy is not None:
        label, icon = BUSY.get(busy, ("Working…", WORKING_ICON))
        return Slot(label, icon, None, None, False, label)
    if phase == STOPPING:
        return Slot("Shutting Down…", SHUTDOWN_ICON, None, None, False, "Shutting Down…")
    if phase not in (STARTING, RUNNING):
        return Slot("Start", START_ICON, "start", "start_menu", True, blocked or "Start")
    if display != "dbus":
        return Slot(
            "Shut Down…",
            SHUTDOWN_ICON,
            "shutdown",
            "unplug_menu",
            False,
            f"Shut Down… (display {display}: the viewer needs dbus)",
        )
    if viewer:
        return Slot("Shut Down…", SHUTDOWN_ICON, "shutdown", "viewer_menu", False, "Shut Down…")
    if not pid:  # no QEMU to connect to yet
        return Slot("Shut Down…", SHUTDOWN_ICON, "shutdown", "unplug_menu", False, "Shut Down…")
    return Slot("Connect", CONNECT_ICON, "connect", "running_menu", True, "Connect")
