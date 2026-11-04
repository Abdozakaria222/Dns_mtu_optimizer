# -*- coding: utf-8 -*-
"""Net Optimizer (Android) - Kivy UI. All measuring lives in netcore.py."""

import math
import os
import threading
import traceback

from kivy.app import App
from kivy.clock import Clock
from kivy.core.clipboard import Clipboard
from kivy.core.window import Window
from kivy.graphics import Color, RoundedRectangle
from kivy.metrics import dp, sp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.progressbar import ProgressBar
from kivy.uix.scrollview import ScrollView
from kivy.uix.spinner import Spinner
from kivy.uix.textinput import TextInput
from kivy.uix.togglebutton import ToggleButton
from kivy.utils import get_color_from_hex as hexc, platform

import netcore as nc

BG = hexc("#0b1220")
CARD = hexc("#131c2f")
CARD2 = hexc("#1b2640")
ACCENT = hexc("#38bdf8")
TEXT = hexc("#e5e7eb")
MUTED = hexc("#94a3b8")
GREEN = hexc("#22c55e")
RED = hexc("#ef4444")
BEST_BG = hexc("#12391f")
DARK = hexc("#04121f")

Window.clearcolor = BG
try:
    Window.softinput_mode = "below_target"  # keep the focused text box above the keyboard
except Exception:  # noqa: BLE001
    pass


# ----------------------------------------------------------------------------
# Android helpers (all wrapped: they must never crash the app)
# ----------------------------------------------------------------------------
def _activity():
    from jnius import autoclass
    return autoclass("org.kivy.android.PythonActivity").mActivity


def android_files_dir():
    """App folder on shared storage (visible to file managers on most phones)."""
    try:
        d = _activity().getExternalFilesDir(None)
        return d.getAbsolutePath() if d else None
    except Exception:  # noqa: BLE001
        return None


def open_wifi_settings():
    if platform != "android":
        return False
    try:
        from jnius import autoclass
        Intent = autoclass("android.content.Intent")
        Settings = autoclass("android.provider.Settings")
        _activity().startActivity(Intent(Settings.ACTION_WIFI_SETTINGS))
        return True
    except Exception:  # noqa: BLE001
        return False


def share_text(subject, text):
    """Android share sheet (send the CSV to Drive / WhatsApp / mail / notes...)."""
    if platform != "android":
        return False
    try:
        from jnius import autoclass, cast
        Intent = autoclass("android.content.Intent")
        String = autoclass("java.lang.String")
        intent = Intent()
        intent.setAction(Intent.ACTION_SEND)
        intent.putExtra(Intent.EXTRA_SUBJECT, cast("java.lang.CharSequence", String(subject)))
        intent.putExtra(Intent.EXTRA_TEXT, cast("java.lang.CharSequence", String(text)))
        intent.setType("text/plain")
        _activity().startActivity(Intent.createChooser(intent, cast("java.lang.CharSequence", String(subject))))
        return True
    except Exception:  # noqa: BLE001
        return False


# ----------------------------------------------------------------------------
# small widgets
# ----------------------------------------------------------------------------
class Card(GridLayout):
    """Rounded dark panel that grows with its content."""

    def __init__(self, bg=CARD, **kw):
        kw.setdefault("cols", 1)
        kw.setdefault("padding", dp(12))
        kw.setdefault("spacing", dp(8))
        kw.setdefault("size_hint_y", None)
        super().__init__(**kw)
        self.bind(minimum_height=self.setter("height"))
        with self.canvas.before:
            self._color = Color(*bg)
            self._rect = RoundedRectangle(pos=self.pos, size=self.size, radius=[dp(10)])
        self.bind(pos=self._sync, size=self._sync)

    def _sync(self, *_):
        self._rect.pos, self._rect.size = self.pos, self.size

    def set_bg(self, rgba):
        self._color.rgba = rgba


class WLabel(Label):
    """Label that wraps to its width and sizes its own height."""

    def __init__(self, **kw):
        kw.setdefault("halign", "left")
        kw.setdefault("valign", "middle")
        kw.setdefault("markup", True)
        kw.setdefault("color", TEXT)
        kw.setdefault("font_size", sp(14))
        kw.setdefault("size_hint_y", None)
        super().__init__(**kw)
        self.bind(width=lambda *_: setattr(self, "text_size", (self.width, None)),
                  texture_size=lambda *_: setattr(self, "height", max(self.texture_size[1] + dp(4), dp(22))))


class Btn(Button):
    def __init__(self, accent=False, danger=False, **kw):
        kw.setdefault("size_hint_y", None)
        kw.setdefault("height", dp(46))
        kw.setdefault("background_normal", "")
        kw.setdefault("background_down", "")
        kw.setdefault("font_size", sp(14))
        kw["background_color"] = ACCENT if accent else (hexc("#7f1d1d") if danger else CARD2)
        kw["color"] = DARK if accent else TEXT
        kw["bold"] = accent
        super().__init__(**kw)


class RowButton(Button):
    """A result row (tap to select)."""

    def __init__(self, **kw):
        kw.setdefault("markup", True)
        kw.setdefault("halign", "left")
        kw.setdefault("valign", "middle")
        kw.setdefault("size_hint_y", None)
        kw.setdefault("height", dp(66))
        kw.setdefault("background_normal", "")
        kw.setdefault("background_down", "")
        kw.setdefault("background_color", CARD)
        kw.setdefault("font_size", sp(13))
        kw.setdefault("padding", (dp(12), dp(4)))
        kw["color"] = TEXT
        super().__init__(**kw)
        self.bind(size=lambda *_: setattr(self, "text_size", (self.width - dp(24), self.height)))


def field(label, default, int_only=False, hint=""):
    row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
    row.add_widget(Label(text=label, color=MUTED, halign="left", valign="middle", size_hint_x=0.55,
                         font_size=sp(13), text_size=(0, None)))
    row.children[0].bind(size=lambda w, _: setattr(w, "text_size", (w.width, w.height)))
    ti = TextInput(text=str(default), multiline=False, hint_text=hint, size_hint_x=0.45,
                   input_filter="int" if int_only else None, background_color=CARD2,
                   foreground_color=TEXT, cursor_color=ACCENT, hint_text_color=MUTED,
                   padding=(dp(10), dp(12)), font_size=sp(14))
    row.add_widget(ti)
    return row, ti


def as_int(ti, default, lo, hi):
    try:
        return max(lo, min(hi, int(ti.text)))
    except (TypeError, ValueError):
        return default


class LogView(WLabel):
    def __init__(self, **kw):
        kw.setdefault("color", MUTED)
        kw.setdefault("font_size", sp(11))
        kw["markup"] = False
        super().__init__(**kw)
        self.lines = []

    def add(self, msg):
        self.lines.append(msg)
        self.lines = self.lines[-40:]
        self.text = "\n".join(self.lines)


def page():
    """ScrollView + vertical column. Returns (scrollview, column)."""
    sv = ScrollView(do_scroll_x=False, bar_width=dp(3))
    col = GridLayout(cols=1, size_hint_y=None, spacing=dp(10), padding=dp(10))
    col.bind(minimum_height=col.setter("height"))
    sv.add_widget(col)
    return sv, col


# ----------------------------------------------------------------------------
# DNS tab
# ----------------------------------------------------------------------------
class DnsTab(BoxLayout):
    def __init__(self, app, **kw):
        super().__init__(orientation="vertical", **kw)
        self.app = app
        self.stop_evt = threading.Event()
        self.running = False
        self.results = []
        self.best = None
        self.selected = None      # result dict the user tapped
        self.csv_text = ""

        sv, col = page()
        self.add_widget(sv)

        c = Card()
        c.add_widget(WLabel(text="[b]Source[/b]", color=MUTED))
        self.source = Spinner(text="Built-in list",
                              values=("Built-in list", "Online list (public-dns.info)"),
                              size_hint_y=None, height=dp(44), background_normal="",
                              background_color=CARD2, color=TEXT, font_size=sp(14))
        c.add_widget(self.source)
        r, self.countries = field("Countries (EG,SA or ALL)", "EG")
        c.add_widget(r)
        r, self.scan_max = field("Scan up to (online)", 200, True)
        c.add_widget(r)
        r, self.top_n = field("Full-test the top", 15, True)
        c.add_widget(r)
        r, self.rounds = field("Rounds per domain", 4, True)
        c.add_widget(r)
        row = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(8))
        self.btn_start = Btn(text="Find best DNS", accent=True, on_release=lambda *_: self.start())
        self.btn_stop = Btn(text="Stop", danger=True, disabled=True, on_release=lambda *_: self.stop_evt.set())
        row.add_widget(self.btn_start)
        row.add_widget(self.btn_stop)
        c.add_widget(row)
        self.prog = ProgressBar(max=1, value=0, size_hint_y=None, height=dp(8))
        c.add_widget(self.prog)
        col.add_widget(c)

        self.best_card = Card()
        self.best_title = WLabel(text="Best DNS: -", font_size=sp(20), color=GREEN)
        self.best_detail = WLabel(text="Latency: -  |  Jitter: -  |  Loss: -")
        self.best_help = WLabel(text="", color=MUTED, font_size=sp(12))
        self.best_card.add_widget(self.best_title)
        self.best_card.add_widget(self.best_detail)
        self.best_card.add_widget(self.best_help)
        r1 = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(8))
        r1.add_widget(Btn(text="Copy DNS 1", on_release=lambda *_: self.copy_ip(1)))
        r1.add_widget(Btn(text="Copy DNS 2", on_release=lambda *_: self.copy_ip(2)))
        self.best_card.add_widget(r1)
        r2 = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(8))
        r2.add_widget(Btn(text="Copy Private DNS name", on_release=lambda *_: self.copy_private()))
        r2.add_widget(Btn(text="Wi-Fi settings", accent=True, on_release=lambda *_: self.wifi()))
        self.best_card.add_widget(r2)
        self.best_card.add_widget(Btn(text="Share results (CSV)", on_release=lambda *_: self.share()))
        self.csv_label = WLabel(text="CSV: -", color=MUTED, font_size=sp(11))
        self.best_card.add_widget(self.csv_label)
        col.add_widget(self.best_card)

        self.rows = GridLayout(cols=1, size_hint_y=None, spacing=dp(6))
        self.rows.bind(minimum_height=self.rows.setter("height"))
        col.add_widget(WLabel(text="[b]Results[/b]  (tap a row to pick it)", color=MUTED))
        col.add_widget(self.rows)

        self.log = LogView(text="")
        col.add_widget(self.log)
        app.logs.append(self.log)

    # ---- actions ----
    def target(self):
        return self.selected or self.best

    def copy_ip(self, n):
        t = self.target()
        ip = t and (t["ip1"] if n == 1 else t["ip2"])
        if not ip:
            self.app.say("No DNS %d for this server" % n if t else "Run the test first")
            return
        Clipboard.copy(ip)
        self.app.say("Copied " + ip)

    def copy_private(self):
        t = self.target()
        host = t and nc.PRIVATE_DNS_HOST.get(t["ip1"])
        if not host:
            self.app.say("No Private DNS name for this server - use the IPs on Wi-Fi" if t else "Run the test first")
            return
        Clipboard.copy(host)
        self.app.say("Copied " + host)

    def wifi(self):
        if not open_wifi_settings():
            self.app.say("Open Settings > Wi-Fi manually")

    def share(self):
        if not self.csv_text:
            self.app.say("Run the test first")
            return
        if not share_text("Net Optimizer - DNS results", self.csv_text):
            Clipboard.copy(self.csv_text)
            self.app.say("CSV copied to the clipboard")

    # ---- run ----
    def start(self):
        if self.running or self.app.busy:
            return
        self.stop_evt.clear()
        self.results, self.best, self.selected, self.csv_text = [], None, None, ""
        self.rows.clear_widgets()
        self.best_title.text = "Best DNS: testing..."
        self.best_detail.text = "Latency: -  |  Jitter: -  |  Loss: -"
        self.best_help.text = ""
        self.csv_label.text = "CSV: -"
        self.prog.value = 0
        online = self.source.text.startswith("Online")
        opts = dict(
            source="online" if online else "builtin",
            countries=[c for c in self.countries.text.upper().replace(";", ",").replace(" ", ",").split(",")
                       if c and c != "ALL"],
            scan_max=as_int(self.scan_max, 200, 20, 3000),
            top_n=as_int(self.top_n, 15, 3, 60),
            rounds=as_int(self.rounds, 4, 2, 12),
        )
        self.set_running(True)
        threading.Thread(target=self.worker, args=(opts,), daemon=True).start()

    def set_running(self, on):
        self.running = on
        self.app.busy = on
        self.btn_start.disabled = on
        self.btn_stop.disabled = not on

    def worker(self, opts):
        ui = self.app.ui
        try:
            res = nc.run_dns_test(opts, self.stop_evt, lambda kind, *a: ui(self.on_event, kind, *a))
            path, text = (None, "")
            if res["results"]:
                path, text = nc.save_dns_csv(res["results"], res["scan_rows"], res["stopped"])
            ui(self.finished, res, path, text, None)
        except nc.TestError as exc:
            ui(self.finished, None, None, "", str(exc))
        except Exception as exc:  # noqa: BLE001  (never leave the buttons disabled)
            ui(self.finished, None, None, "", "{}\n{}".format(exc, traceback.format_exc(limit=3)))

    def on_event(self, kind, *a):
        if kind == "log":
            self.app.log(a[0])
        elif kind == "status":
            self.app.say(a[0])
        elif kind == "progress":
            self.prog.max, self.prog.value = max(1, a[1]), a[0]
        elif kind == "dns_row":
            self.results.append(a[0])
            self.render()

    def finished(self, res, path, text, error):
        self.set_running(False)
        self.prog.value = self.prog.max
        if error:
            self.best_title.text = "Best DNS: -"
            self.app.log(error)
            self.app.say(error.splitlines()[0])
            return
        self.render(final=True)
        self.csv_text = text
        if path:
            self.csv_label.text = "CSV saved: " + path
            self.app.log("CSV saved: " + path)
        self.app.say("Stopped (partial results saved)" if res["stopped"] else "DNS test finished")

    def render(self, final=False):
        ranked = nc.rank_dns(self.results)
        self.rows.clear_widgets()
        best = nc.best_dns(self.results)
        for i, r in enumerate(ranked, 1):
            dead = math.isinf(r["score"])
            is_best = best is not None and r is best
            col = "ef4444" if dead else ("22c55e" if is_best else "e5e7eb")
            ips = r["ip1"] + ("  /  " + r["ip2"] if r["ip2"] else "")
            text = ("[b][color=%s]%d. %s[/color][/b]\n%s\n"
                    "[color=94a3b8]Latency %s ms  |  Jitter %s  |  Loss %.0f%%[/color]"
                    % (col, i, nc.dns_label(r), ips, nc.fdisp(r["avg"]), nc.fdisp(r["jit"]), r["loss"]))
            b = RowButton(text=text, height=dp(84),
                          background_color=BEST_BG if is_best else (CARD2 if r is self.selected else CARD))
            b.bind(on_release=lambda _w, rr=r: self.select(rr))
            self.rows.add_widget(b)
        if best:
            self.best = best
            ips = best["ip1"] + ("  /  " + best["ip2"] if best["ip2"] else "")
            self.best_title.text = "Best DNS: %s" % nc.dns_label(best)
            self.best_detail.text = ("%s\nLatency: %s ms (median %s)  |  Jitter: %s ms  |  Loss: %.0f%%"
                                     % (ips, nc.fdisp(best["avg"]), nc.fdisp(best["med"]),
                                        nc.fdisp(best["jit"]), best["loss"]))
            host = nc.PRIVATE_DNS_HOST.get(best["ip1"])
            self.best_help.text = (
                "To use it: Settings > Network > Private DNS > Private DNS provider hostname > [b]%s[/b]\n"
                "or on Wi-Fi: tap your network > IP settings > Static > DNS 1 / DNS 2." % host
                if host else
                "To use it on Wi-Fi: tap your network > IP settings > Static > enter DNS 1 / DNS 2. "
                "(No Private DNS hostname known for this server.)")
        elif final:
            self.best_title.text = "Best DNS: none reachable" if self.results else "Best DNS: -"

    def select(self, r):
        self.selected = r
        self.app.say("Selected: " + nc.dns_label(r))
        self.render()


# ----------------------------------------------------------------------------
# MTU tab
# ----------------------------------------------------------------------------
class MtuTab(BoxLayout):
    def __init__(self, app, **kw):
        super().__init__(orientation="vertical", **kw)
        self.app = app
        self.stop_evt = threading.Event()
        self.running = False
        self.results = []
        self.best = None
        self.csv_text = ""

        sv, col = page()
        self.add_widget(sv)

        c = Card()
        r, self.target = field("Test target", "1.1.1.1")
        c.add_widget(r)
        r, self.count = field("Pings per size", 8, True)
        c.add_widget(r)
        row = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(8))
        self.btn_start = Btn(text="Find best MTU", accent=True, on_release=lambda *_: self.start())
        self.btn_stop = Btn(text="Stop", danger=True, disabled=True, on_release=lambda *_: self.stop_evt.set())
        row.add_widget(self.btn_start)
        row.add_widget(self.btn_stop)
        c.add_widget(row)
        self.prog = ProgressBar(max=24, value=0, size_hint_y=None, height=dp(8))
        c.add_widget(self.prog)
        col.add_widget(c)

        self.best_card = Card()
        self.best_title = WLabel(text="Best MTU: -", font_size=sp(20), color=GREEN)
        self.best_detail = WLabel(text="Latency: -  |  Jitter: -  |  Loss: -")
        self.best_help = WLabel(
            text="Android does not let apps change the MTU. Set the result in your router "
                 "(WAN / LAN MTU), in your VPN app, or with root: [b]ip link set dev wlan0 mtu <value>[/b]",
            color=MUTED, font_size=sp(12))
        self.best_card.add_widget(self.best_title)
        self.best_card.add_widget(self.best_detail)
        self.best_card.add_widget(self.best_help)
        r2 = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(8))
        r2.add_widget(Btn(text="Copy MTU", on_release=lambda *_: self.copy()))
        r2.add_widget(Btn(text="Share results (CSV)", accent=True, on_release=lambda *_: self.share()))
        self.best_card.add_widget(r2)
        self.csv_label = WLabel(text="CSV: -", color=MUTED, font_size=sp(11))
        self.best_card.add_widget(self.csv_label)
        col.add_widget(self.best_card)

        self.rows = GridLayout(cols=1, size_hint_y=None, spacing=dp(6))
        self.rows.bind(minimum_height=self.rows.setter("height"))
        col.add_widget(WLabel(text="[b]Measured sizes[/b]", color=MUTED))
        col.add_widget(self.rows)

        self.log = LogView(text="")
        col.add_widget(self.log)
        app.logs.append(self.log)

    def copy(self):
        if not self.best:
            self.app.say("Run the test first")
            return
        Clipboard.copy(str(self.best["mtu"]))
        self.app.say("Copied " + str(self.best["mtu"]))

    def share(self):
        if not self.csv_text:
            self.app.say("Run the test first")
            return
        if not share_text("Net Optimizer - MTU results", self.csv_text):
            Clipboard.copy(self.csv_text)
            self.app.say("CSV copied to the clipboard")

    def start(self):
        if self.running or self.app.busy:
            return
        target = self.target.text.strip() or "1.1.1.1"
        self.stop_evt.clear()
        self.results, self.best, self.csv_text = [], None, ""
        self.rows.clear_widgets()
        self.best_title.text = "Best MTU: testing..."
        self.best_detail.text = "Latency: -  |  Jitter: -  |  Loss: -"
        self.csv_label.text = "CSV: -"
        self.prog.value = 0
        count = as_int(self.count, 8, 4, 40)
        self.set_running(True)
        threading.Thread(target=self.worker, args=(target, count), daemon=True).start()

    def set_running(self, on):
        self.running = on
        self.app.busy = on
        self.btn_start.disabled = on
        self.btn_stop.disabled = not on

    def worker(self, target, count):
        ui = self.app.ui
        try:
            res = nc.run_mtu_test(target, count, self.stop_evt, lambda kind, *a: ui(self.on_event, kind, *a))
            path, text = nc.save_mtu_csv(res)
            ui(self.finished, res, path, text, None)
        except nc.TestError as exc:
            ui(self.finished, None, None, "", str(exc))
        except Exception as exc:  # noqa: BLE001  (never leave the buttons disabled)
            ui(self.finished, None, None, "", "{}\n{}".format(exc, traceback.format_exc(limit=3)))

    def on_event(self, kind, *a):
        if kind == "log":
            self.app.log(a[0])
        elif kind == "status":
            self.app.say(a[0])
        elif kind == "progress":
            self.prog.max, self.prog.value = a[1], a[0]
        elif kind == "target":
            self.target.text = a[0]
        elif kind == "mtu_search":
            step, payload, ok = a
            self.app.log("Search %d: payload %d (MTU %d) -> %s" % (step, payload, payload + 28,
                                                                  "OK" if ok else "too big"))
        elif kind == "mtu_quality":
            self.results.append(a[0])
            self.render()
        elif kind == "mtu_best":
            self.best = a[0]
            self.render()

    def finished(self, res, path, text, error):
        self.set_running(False)
        if error:
            self.best_title.text = "Best MTU: -"
            self.app.log(error)
            self.app.say(error.splitlines()[0])
            return
        self.prog.value = self.prog.max
        self.best = res["best"]
        self.render()
        if not res["best"]:
            self.best_title.text = "Best MTU: no size got replies"
        self.csv_text = text
        if path:
            self.csv_label.text = "CSV saved: " + path
            self.app.log("CSV saved: " + path)
        self.app.say("Stopped (partial results saved)" if res["stopped"] else "MTU test finished")

    def render(self):
        self.rows.clear_widgets()
        for r in self.results:
            dead = math.isinf(r["score"])
            is_best = self.best is not None and r["mtu"] == self.best["mtu"]
            col = "ef4444" if dead else ("22c55e" if is_best else "e5e7eb")
            text = ("[b][color=%s]MTU %d%s[/color][/b]   (payload %d)\n"
                    "[color=94a3b8]Latency %s ms  |  Jitter %s  |  Loss %.0f%%[/color]"
                    % (col, r["mtu"], "  - BEST" if is_best else "", r["mtu"] - 28,
                       nc.fdisp(r["avg"]), nc.fdisp(r["jit"]), r["loss"]))
            self.rows.add_widget(RowButton(text=text, height=dp(60),
                                           background_color=BEST_BG if is_best else CARD))
        if self.best:
            b = self.best
            self.best_title.text = "Best MTU: %d" % b["mtu"]
            self.best_detail.text = ("Latency: %s ms  |  Jitter: %s ms  |  Loss: %.0f%%"
                                     % (nc.fdisp(b["avg"]), nc.fdisp(b["jit"]), b["loss"]))


# ----------------------------------------------------------------------------
# App
# ----------------------------------------------------------------------------
class NetOptApp(App):
    title = "Net Optimizer"

    def build(self):
        self.busy = False
        self.logs = []

        folder = android_files_dir() if platform == "android" else None
        nc.RESULTS_DIR = os.path.join(folder or self.user_data_dir, "results")

        root = BoxLayout(orientation="vertical")
        head = BoxLayout(size_hint_y=None, height=dp(48), padding=(dp(12), 0))
        head.add_widget(Label(text="[b]Net Optimizer[/b]", markup=True, color=ACCENT, font_size=sp(20),
                              halign="left", valign="middle"))
        head.children[0].bind(size=lambda w, _: setattr(w, "text_size", (w.width, w.height)))
        root.add_widget(head)

        bar = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(4), padding=(dp(10), 0))
        self.tab_btns = {}
        for name in ("DNS", "MTU"):
            tb = ToggleButton(text=name, group="tabs", allow_no_selection=False, background_normal="",
                              background_down="", background_color=CARD, color=MUTED, bold=True,
                              font_size=sp(15), state="down" if name == "DNS" else "normal")
            tb.bind(on_release=lambda w, n=name: self.show(n))
            self.tab_btns[name] = tb
            bar.add_widget(tb)
        root.add_widget(bar)

        self.holder = BoxLayout()
        root.add_widget(self.holder)
        self.tabs = {"DNS": DnsTab(self), "MTU": MtuTab(self)}

        self.status = Label(text="Ready", size_hint_y=None, height=dp(30), color=MUTED, font_size=sp(12),
                            halign="left", valign="middle", padding=(dp(12), 0), shorten=True)
        self.status.bind(size=lambda w, _: setattr(w, "text_size", (w.width, w.height)))
        root.add_widget(self.status)

        self.show("DNS")
        self.log("Results folder: " + nc.RESULTS_DIR)
        return root

    def show(self, name):
        self.holder.clear_widgets()
        self.holder.add_widget(self.tabs[name])
        for n, b in self.tab_btns.items():
            b.state = "down" if n == name else "normal"
            b.background_color = CARD2 if n == name else CARD
            b.color = ACCENT if n == name else MUTED

    # thread-safe call into the UI
    def ui(self, fn, *args):
        Clock.schedule_once(lambda _dt: fn(*args), 0)

    def say(self, msg):
        self.status.text = str(msg)

    def log(self, msg):
        for lv in self.logs:
            lv.add(str(msg))


if __name__ == "__main__":
    NetOptApp().run()
