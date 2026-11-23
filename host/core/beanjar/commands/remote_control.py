# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The TV remote: remote pipe, press, pointer and touch, and the layout."""

import argparse
from .common import DURATION_HELP, Usage, duration, emit, parse_size


def cmd_remote_pipe(lib, a):
    from .. import pipe

    return pipe.remote_pipe(lib.vm(a.name))


def cmd_remote_press(lib, a):
    from .. import pipe

    codes = pipe.press(lib.vm(a.name), a.keys, a.hold)
    return emit(
        a,
        {"name": a.name, "codes": codes},
        f"{a.name}: pressed " + ", ".join(f"{k} (0x{c:02x})" for k, c in zip(a.keys, codes)),
    )


def coordinate(text):
    """A pointer coordinate: a number of frame pixels, whole (640) or a fraction (640.5, an
    element's centre), not negative."""

    try:
        v = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not v >= 0 or v == float("inf"):
        raise argparse.ArgumentTypeError(f"not a coordinate of 0 or more: {text!r}")
    return int(v) if v.is_integer() else v


def cmd_remote_pointer(lib, a):
    from .. import pipe

    frame = parse_size(a.frame)
    if not (1 <= frame[0] <= 16384 and 1 <= frame[1] <= 16384):
        raise Usage(f"invalid frame {a.frame}: 1 to 16384 each way")
    click = a.verb_group == "click"
    data = pipe.pointer(
        lib.vm(a.name),
        a.verb_group,
        a.x,
        a.y,
        frame,
        **({"button": a.button, "settle": a.settle, "hold": a.hold} if click else {}),
    )
    where = (
        f"{data['x']:g},{data['y']:g} of {frame[0]}x{frame[1]} -> "
        f"{data['display_x']},{data['display_y']} of {data['source_width']}x{data['source_height']}"
    )
    text = f"{a.name}: " + (f"{a.button} click at {where}" if click else f"pointer to {where}")
    return emit(a, {"name": a.name} | data, text)


def cmd_remote_touch(lib, a):
    from .. import pipe

    frame = parse_size(a.frame)
    if not (1 <= frame[0] <= 16384 and 1 <= frame[1] <= 16384):
        raise Usage(f"invalid frame {a.frame}: 1 to 16384 each way")
    tap = a.verb_group == "tap"
    if tap:
        data = pipe.touch(lib.vm(a.name), [(a.x, a.y)], frame, hold=a.hold)
    else:
        if not 1 <= a.steps <= 1000:
            raise Usage("--steps takes 1 to 1000")
        data = pipe.touch(
            lib.vm(a.name),
            [(a.x, a.y), (a.x2, a.y2)],
            frame,
            duration=a.time,
            steps=a.steps,
        )
    where = " -> ".join(f"{x:g},{y:g}" for x, y in data["points"])
    shown = " -> ".join(f"{x},{y}" for x, y in data["display_points"])
    text = (
        f"{a.name}: {data['action']} at {where} of {frame[0]}x{frame[1]} -> {shown} of "
        f"{data['source_width']}x{data['source_height']}"
    )
    return emit(a, {"name": a.name} | data, text)


def cmd_remote_layout(lib, a):
    from .. import pipe, remote_layouts

    layout = pipe.vm_layout(lib.vm(a.name))
    data = remote_layouts.as_json(layout)
    dim = sum(1 for k in layout.keys if k.code is None)
    return emit(
        a, data, f"{a.name}: {layout.name} ({layout.title}), {len(layout.keys)} keys, {dim} dimmed"
    )


def register(cli):
    command, group, vm_name = cli.command, cli.group, cli.vm_name
    g = group(
        "remote",
        "the TV remote's buttons (IR), the Magic Remote's pointer and the touch screen through the "
        "display's front end",
    )
    s = command(
        g,
        "pipe",
        "remote button edges from stdin, a line each: 'down CODE' or 'up CODE', CODE an LG IR "
        "code (0x7c) or a key of the VM's remote (HOME); answers 'ready', and 'error: ...' for a "
        "line it cannot do; at the end of stdin what is held is released. Needs beanjar-vnc or the "
        "viewer (org.beanjar.Display1.Ir)",
        cmd_remote_pipe,
    )
    vm_name(s, "pipe")
    s = command(
        g,
        "press",
        "press remote keys in turn (names of the VM's remote, e.g. HOME, UP, ENTER, or IR codes)",
        cmd_remote_press,
        usage="beanjar remote press NAME KEY... [--hold DURATION] [--json]",
    )
    vm_name(s)
    s.add_argument("keys", nargs="+", metavar="KEY")
    s.add_argument(
        "--hold",
        type=duration,
        default=0.1,
        metavar="DURATION",
        help=f"how long each is held (default 100ms); {DURATION_HELP}",
    )
    for verb, text in (
        (
            "click",
            (
                "click the Magic Remote pointer at X Y of a screenshot (the frame, default "
                "1280x720, that screenshot and elements report boxes in): a wake movement, the "
                "pointer to X Y, a wait (--settle), the button held (--hold). Needs beanjar-vnc or the "
                "viewer (org.beanjar.Display1.Pointer)"
            ),
        ),
        (
            "move",
            (
                "move the Magic Remote pointer to X Y of a screenshot (default frame 1280x720), with "
                "the wake movement that shows LG's cursor. Needs beanjar-vnc or the viewer"
            ),
        ),
    ):
        s = command(
            g,
            verb,
            text,
            cmd_remote_pointer,
            usage=f"beanjar remote {verb} NAME X Y [--frame WxH]"
            + (" [--button B] [--settle DURATION] [--hold DURATION]" if verb == "click" else "")
            + " [--json]",
        )
        vm_name(s)
        s.add_argument("x", type=coordinate, metavar="X")
        s.add_argument("y", type=coordinate, metavar="Y")
        s.add_argument(
            "--frame",
            default="1280x720",
            metavar="WxH",
            help="the screenshot frame X Y are in (screenshot --frame; default 1280x720)",
        )
        if verb == "click":
            s.add_argument(
                "--button",
                choices=("left", "middle", "right"),
                default="left",
                help="default left (the Magic Remote's OK)",
            )
            s.add_argument(
                "--settle",
                type=duration,
                default=0.3,
                metavar="DURATION",
                help=f"wait after the move, before the press (default 300ms); {DURATION_HELP}",
            )
            s.add_argument(
                "--hold",
                type=duration,
                default=0.1,
                metavar="DURATION",
                help=f"how long the button is held (default 100ms); {DURATION_HELP}",
            )
    for verb, text in (
        (
            "tap",
            "tap the touch screen (a model with one, such as the StanbyME) at X Y of a screenshot "
            "(default frame 1280x720): one finger down for --hold. Needs beanjar-vnc or the viewer "
            "(org.beanjar.Display1.Touch)",
        ),
        (
            "swipe",
            "swipe one finger on the touch screen from X Y to X2 Y2 of a screenshot (default "
            "frame 1280x720) in --time. Needs beanjar-vnc or the viewer",
        ),
    ):
        s = command(
            g,
            verb,
            text,
            cmd_remote_touch,
            usage=f"beanjar remote {verb} NAME X Y"
            + (" X2 Y2 [--time DURATION] [--steps N]" if verb == "swipe" else " [--hold DURATION]")
            + " [--frame WxH] [--json]",
        )
        vm_name(s)
        s.add_argument("x", type=coordinate, metavar="X")
        s.add_argument("y", type=coordinate, metavar="Y")
        if verb == "swipe":
            s.add_argument("x2", type=coordinate, metavar="X2")
            s.add_argument("y2", type=coordinate, metavar="Y2")
            s.add_argument(
                "--time",
                type=duration,
                default=0.3,
                metavar="DURATION",
                help=f"how long the swipe takes (default 300ms); {DURATION_HELP}",
            )
            s.add_argument(
                "--steps", type=int, default=10, metavar="N", help="moves along it (default 10)"
            )
        else:
            s.add_argument(
                "--hold",
                type=duration,
                default=0.1,
                metavar="DURATION",
                help=f"how long the finger is down (default 100ms); {DURATION_HELP}",
            )
        s.add_argument(
            "--frame",
            default="1280x720",
            metavar="WxH",
            help="the screenshot frame the points are in (screenshot --frame; default 1280x720)",
        )
    s = command(
        g,
        "layout",
        "the VM's remote (the viewer's layout for its firmware): with --json its keys, boxes, "
        "colours and IR codes",
        cmd_remote_layout,
    )
    vm_name(s)
