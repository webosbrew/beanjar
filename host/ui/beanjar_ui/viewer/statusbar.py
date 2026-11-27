# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The status bar: CPU, GPU, memory, Disk and Network indicators on the right with numbers in
tooltips (the layout is ui/statusbar.blp and ui/statusfield.blp), styled as a libadwaita bottom bar. The Remote toggle and the window's buttons are in the window's header
bar. Nothing in it takes keyboard focus."""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, Graphene, Gtk  # noqa: E402

from . import stats as statslib  # noqa: E402
from . import statusfmt as fmt  # noqa: E402
from . import ui  # noqa: E402

# libadwaita's colours (its CSS variables): the bar is coloured as a header bar, as a
# ToolbarView's bottom bar would be. An indicator is dimmed idle, dimmer unavailable, and in its
# colour while its counter moves (memory: while measured): CPU the success colour (green), GPU and
# memory the purple and blue accents, made readable on the bar as libadwaita makes its own
# standalone colours (--standalone-color-oklab: darker on light, lighter on dark).
CSS = """
.wv-statusbar { padding: 2px 10px; min-height: 22px; background-color: var(--headerbar-bg-color);
                color: var(--headerbar-fg-color); box-shadow: inset 0 1px var(--headerbar-shade-color); }
.wv-statusbar .field { margin-left: 10px; }
.wv-statusbar .field image, .wv-statusbar .field .meter { opacity: 0.55; }
.wv-statusbar .field.active image, .wv-statusbar .field.active .meter {
  opacity: 1; color: var(--success-color); }
.wv-statusbar .field.active.gpu .meter {
  color: oklab(from var(--accent-purple) var(--standalone-color-oklab)); }
.wv-statusbar .field.active.memory .meter {
  color: oklab(from var(--accent-blue) var(--standalone-color-oklab)); }
.wv-statusbar .field.off image, .wv-statusbar .field.off .meter { opacity: 0.25; }
"""
KINDS = ("cpu", "gpu", "memory", "disk", "net")  # a field's style class, in statslib's order


class Meter(Gtk.Widget):
    """A small tray-style meter: one column per measured vCPU, the guest's RAM and (where it has
    any) swap, or the aggregate (CPU without vCPUs, GPU) if unavailable."""

    __gtype_name__ = "ViewerMeter"

    def __init__(self):
        super().__init__(valign=Gtk.Align.CENTER)
        self.add_css_class("meter")
        self.values = (None,)

    def update(self, src):
        self.values = (
            tuple(value for _, value in src.cores)
            or (src.bars if src.ok else ())
            or (src.a if src.ok else None,)
        )
        self.queue_resize()
        self.queue_draw()

    def do_measure(self, orientation, for_size):
        size = (
            max(16, min(64, 3 * len(self.values) + 2))
            if orientation == Gtk.Orientation.HORIZONTAL
            else 16
        )
        return size, size, -1, -1

    def do_snapshot(self, snapshot):
        width, height = self.get_width(), self.get_height()
        color = self.get_color()
        background = Gdk.RGBA()
        background.red, background.green, background.blue = color.red, color.green, color.blue
        background.alpha = color.alpha * 0.2

        def rect(ink, x, y, w, h):
            snapshot.append_color(ink, Graphene.Rect().init(x, y, w, h))

        rect(background, 0, 0, width, 1)
        rect(background, 0, height - 1, width, 1)
        rect(background, 0, 0, 1, height)
        rect(background, width - 1, 0, 1, height)
        step = (width - 4) / len(self.values)
        for index, value in enumerate(self.values):
            x, w = 2 + index * step, max(0.5, step - 1)
            rect(background, x, 2, w, height - 4)
            if value is not None:
                filled = max(1, round((height - 4) * max(0, min(100, value)) / 100))
                rect(color, x, height - 2 - filled, w, filled)


@ui.template("statusfield")
class StatusField(Gtk.Box):
    """One indicator (ui/statusfield.blp): an icon by state, its numbers in the tooltip."""

    __gtype_name__ = "ViewerStatusField"
    icon = Gtk.Template.Child()

    def __init__(self):
        super().__init__()
        self.meter = None

    def update(self, src, kind):
        st = fmt.state(src)
        if kind in fmt.METERS:
            if self.meter is None:
                self.meter = Meter()
                self.append(self.meter)
                self.icon.set_visible(False)
            self.meter.update(src)
        else:
            self.icon.set_from_icon_name(fmt.ICONS[kind][st])
        self.set_css_classes(["field", st, KINDS[kind]])
        self.set_tooltip_text(fmt.tooltip(kind, src))


@ui.template("statusbar")
class StatusBar(Gtk.Box):
    """The bar (ui/statusbar.blp). stats: the pane's statslib.Stats (None: no stats); update()
    shows its numbers."""

    __gtype_name__ = "ViewerStatusBar"
    cpu = Gtk.Template.Child()
    gpu = Gtk.Template.Child()
    memory = Gtk.Template.Child()
    disk = Gtk.Template.Child()
    net = Gtk.Template.Child()

    def __init__(self, stats):
        super().__init__()
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        # in statslib's order
        self.fields = [self.cpu, self.gpu, self.memory, self.disk, self.net]
        self.stats = stats
        self.update()

    def update(self):
        for kind, f in enumerate(self.fields):
            f.update(self.stats.src[kind] if self.stats else statslib.Source(), kind)
            if not self.stats:
                f.set_tooltip_text(f"{fmt.TITLES[kind]}: no socket directory for this bus address")
