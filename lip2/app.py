from __future__ import annotations

import re
import sys
import threading
import time
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import gi
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

from lip2.api import LipserviceAPI, APIError  # noqa: E402
from lip2.irc_format import parse as parse_irc  # noqa: E402


def _run_in_thread(
    func: Callable[[], Any],
    callback: Callable[..., Any] | None = None,
    error_callback: Callable[..., Any] | None = None,
) -> None:
    def worker() -> None:
        try:
            result = func()
            if callback:
                GLib.idle_add(callback, result)
        except Exception as exc:
            if error_callback:
                GLib.idle_add(error_callback, exc)

    threading.Thread(target=worker, daemon=True).start()


# -- Sidebar row -------------------------------------------------------------

class SidebarRow(Gtk.ListBoxRow):
    def __init__(
        self, network: str, channel: str | None = None,
        query: str | None = None, state: str = "",
    ) -> None:
        super().__init__()
        self.network = network
        self.channel = channel
        self.query = query
        self.net_state = state
        self._unread = False
        self._label = Gtk.Label()
        self._label.set_xalign(0)
        self._label.set_margin_start(20 if (channel or query) else 8)
        self._label.set_margin_end(8)
        self._label.set_margin_top(3)
        self._label.set_margin_bottom(3)
        self.update(state)
        self.set_child(self._label)
        if not channel and not query:
            self.set_selectable(False)

    @property
    def is_selectable_target(self) -> bool:
        return self.channel is not None or self.query is not None

    def set_unread(self, unread: bool) -> None:
        if not self.is_selectable_target:
            return
        self._unread = unread
        name = GLib.markup_escape_text(self.channel or self.query or "")
        if self.query:
            name = f"<i>{name}</i>"
        if unread:
            self._label.set_markup(f"<b>{name}</b>")
        else:
            self._label.set_markup(name)

    def update(self, state: str = "") -> None:
        self.net_state = state
        if self.channel:
            self._label.set_text(self.channel)
        elif self.query:
            self._label.set_markup(
                f"<i>{GLib.markup_escape_text(self.query)}</i>"
            )
        else:
            suffix = ""
            if state and state != "connected":
                suffix = (
                    f"  <small>({GLib.markup_escape_text(state)})</small>"
                )
            self._label.set_markup(
                f"<b>{GLib.markup_escape_text(self.network)}</b>{suffix}"
            )


# -- Config -------------------------------------------------------------------

_CONFIG_DIR = Path.home() / ".config" / "lip2"
_CONFIG_FILE = _CONFIG_DIR / "config.toml"

_DEFAULTS = {
    "url": "http://127.0.0.1:8080/api",
    "username": "admin",
}


def _load_config() -> dict[str, str]:
    cfg: dict[str, str] = dict(_DEFAULTS)
    try:
        with open(_CONFIG_FILE, "rb") as f:
            cfg.update(tomllib.load(f))
    except FileNotFoundError:
        pass
    return cfg


def _save_config(url: str, username: str) -> None:
    cfg = _load_config()
    cfg["url"] = url
    cfg["username"] = username
    _save_config_dict(cfg)


def _save_config_dict(cfg: dict[str, str]) -> None:
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lines = [f'{k} = "{v}"\n' for k, v in cfg.items()]
    _CONFIG_FILE.write_text("".join(lines))


# -- Login window -------------------------------------------------------------

class LoginWindow(Gtk.Window):
    def __init__(self, app: Lip2App) -> None:
        super().__init__(title="Lip2", application=app)
        self._app = app
        self.set_default_size(360, 0)
        self.set_resizable(False)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(20)
        box.set_margin_bottom(20)
        box.set_margin_start(20)
        box.set_margin_end(20)

        cfg = _load_config()

        box.append(self._field_label("Proxy URL"))
        self._url = Gtk.Entry()
        self._url.set_text(cfg.get("url", ""))
        box.append(self._url)

        box.append(self._field_label("Username"))
        self._user = Gtk.Entry()
        self._user.set_text(cfg.get("username", ""))
        box.append(self._user)

        box.append(self._field_label("Password"))
        self._pass = Gtk.Entry()
        self._pass.set_visibility(False)
        self._pass.connect("activate", self._on_login)
        box.append(self._pass)

        self._error = Gtk.Label()
        self._error.set_wrap(True)
        self._error.set_visible(False)
        box.append(self._error)

        self._btn = Gtk.Button(label="Login")
        self._btn.connect("clicked", self._on_login)
        self._btn.set_margin_top(8)
        box.append(self._btn)

        self.set_child(box)

    @staticmethod
    def _field_label(text: str) -> Gtk.Label:
        lbl = Gtk.Label(label=text)
        lbl.set_xalign(0)
        return lbl

    def _on_login(self, _widget: Gtk.Widget) -> None:
        url = self._url.get_text().strip()
        user = self._user.get_text().strip()
        pw = self._pass.get_text().strip()
        if not url or not user:
            return

        self._btn.set_sensitive(False)
        self._error.set_visible(False)

        def attempt() -> LipserviceAPI:
            api = LipserviceAPI(url)
            api.login(user, pw)
            return api

        def on_ok(api: LipserviceAPI) -> None:
            _save_config(url, user)
            self._app.api = api
            self.close()
            self._app.open_main_window()

        def on_err(exc: Exception) -> None:
            self._btn.set_sensitive(True)
            msg = exc.message if isinstance(exc, APIError) else str(exc)
            self._error.set_text(msg)
            self._error.set_visible(True)

        _run_in_thread(attempt, on_ok, on_err)


# -- Formatted input ----------------------------------------------------------

_TOGGLE_TAGS: dict[int, str] = {
    Gdk.KEY_b: "irc_bold",
    Gdk.KEY_i: "irc_italic",
    Gdk.KEY_u: "irc_underline",
}

_TAG_TO_IRC: dict[str, str] = {
    "irc_bold": "\x02",
    "irc_italic": "\x1D",
    "irc_underline": "\x1F",
    "irc_strike": "\x1E",
    "irc_mono": "\x11",
}

_URL_RE = re.compile(r"https?://[^\s<>]+(?<![.,;:!?\"')>])")


class FormattedInput(Gtk.Frame):
    """Single-line WYSIWYG input with IRC formatting support.

    Signals:
        send: emitted when the user presses Enter.
    """

    __gsignals__ = {
        "send": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self) -> None:
        super().__init__()
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        toolbar.set_margin_start(4)
        toolbar.set_margin_top(2)
        toolbar.set_margin_bottom(2)
        self._toggle_buttons: dict[str, Gtk.ToggleButton] = {}
        for tag_name, label, tooltip in (
            ("irc_bold", "B", "Bold (Ctrl+B)"),
            ("irc_italic", "I", "Italic (Ctrl+I)"),
            ("irc_underline", "U", "Underline (Ctrl+U)"),
        ):
            btn = Gtk.ToggleButton(label=label)
            btn.set_has_frame(False)
            btn.set_focusable(False)
            btn.set_tooltip_text(tooltip)
            btn.connect("toggled", self._on_toolbar_toggle, tag_name)
            toolbar.append(btn)
            self._toggle_buttons[tag_name] = btn
        # Style the labels to hint at their function
        bold_btn = self._toggle_buttons["irc_bold"]
        bold_btn.get_child().set_markup("<b>B</b>")
        italic_btn = self._toggle_buttons["irc_italic"]
        italic_btn.get_child().set_markup("<i>I</i>")
        underline_btn = self._toggle_buttons["irc_underline"]
        underline_btn.get_child().set_markup("<u>U</u>")

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        toolbar.append(spacer)

        self.night_btn = Gtk.ToggleButton(label="\u263e")
        self.night_btn.set_has_frame(False)
        self.night_btn.set_focusable(False)
        self.night_btn.set_tooltip_text("Toggle night mode")
        toolbar.append(self.night_btn)

        vbox.append(toolbar)

        self._view = Gtk.TextView()
        self._view.set_wrap_mode(Gtk.WrapMode.NONE)
        self._view.set_accepts_tab(False)
        self._view.set_top_margin(4)
        self._view.set_bottom_margin(4)
        self._view.set_left_margin(6)
        self._view.set_right_margin(6)

        self._ibuf = self._view.get_buffer()
        self._ibuf.create_tag("irc_bold", weight=Pango.Weight.BOLD)
        self._ibuf.create_tag("irc_italic", style=Pango.Style.ITALIC)
        self._ibuf.create_tag(
            "irc_underline", underline=Pango.Underline.SINGLE,
        )
        self._ibuf.create_tag("irc_strike", strikethrough=True)
        self._ibuf.create_tag("irc_mono", family="Monospace")

        self._active_tags: set[str] = set()
        self._updating_buttons = False
        self._max_bytes = 400
        self._ibuf.connect("insert-text", self._on_insert_text)

        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect("key-pressed", self._on_key)
        self._view.add_controller(key_ctrl)

        vbox.append(self._view)
        self.set_child(vbox)

    def get_irc_text(self) -> str:
        """Serialize the buffer contents to IRC-formatted text."""
        start = self._ibuf.get_start_iter()
        end = self._ibuf.get_end_iter()
        if start.equal(end):
            return ""
        return self._serialize(start, end)

    def clear(self) -> None:
        self._ibuf.set_text("")
        self._active_tags.clear()
        self._sync_buttons()

    def grab_focus(self) -> bool:
        return self._view.grab_focus()

    def _sync_buttons(self) -> None:
        self._updating_buttons = True
        for tag_name, btn in self._toggle_buttons.items():
            btn.set_active(tag_name in self._active_tags)
        self._updating_buttons = False

    def _toggle_tag(self, tag_name: str) -> None:
        if tag_name in self._active_tags:
            self._active_tags.discard(tag_name)
        else:
            self._active_tags.add(tag_name)
        self._apply_to_selection(tag_name)
        self._sync_buttons()

    def _on_toolbar_toggle(
        self, btn: Gtk.ToggleButton, tag_name: str,
    ) -> None:
        if self._updating_buttons:
            return
        self._toggle_tag(tag_name)
        self._view.grab_focus()

    def _on_key(
        self, _ctrl: Gtk.EventControllerKey,
        keyval: int, _keycode: int, state: Gdk.ModifierType,
    ) -> bool:
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if not (state & (Gdk.ModifierType.SHIFT_MASK
                             | Gdk.ModifierType.CONTROL_MASK)):
                self.emit("send")
                return True

        if not (state & Gdk.ModifierType.CONTROL_MASK):
            return False

        tag_name = _TOGGLE_TAGS.get(keyval)
        if tag_name is not None:
            self._toggle_tag(tag_name)
            return True

        if keyval == Gdk.KEY_o:
            self._active_tags.clear()
            self._clear_selection_tags()
            self._sync_buttons()
            return True

        return False

    def _on_insert_text(
        self, buf: Gtk.TextBuffer, loc: Gtk.TextIter,
        text: str, _length: int,
    ) -> None:
        current = buf.get_text(
            buf.get_start_iter(), buf.get_end_iter(), False,
        )
        if len(current.encode("utf-8")) + len(text.encode("utf-8")) \
                > self._max_bytes:
            GObject.signal_stop_emission_by_name(buf, "insert-text")
            return

        if not self._active_tags:
            return

        tags_snapshot = set(self._active_tags)
        offset = loc.get_offset()
        text_len = len(text)

        def apply_after() -> bool:
            start = buf.get_iter_at_offset(offset)
            end = buf.get_iter_at_offset(offset + text_len)
            for tag_name in tags_snapshot:
                tag = buf.get_tag_table().lookup(tag_name)
                if tag:
                    buf.apply_tag(tag, start, end)
            return False

        GLib.idle_add(apply_after)

    def _apply_to_selection(self, tag_name: str) -> None:
        sel = self._ibuf.get_selection_bounds()
        if not sel:
            return
        start, end = sel
        tag = self._ibuf.get_tag_table().lookup(tag_name)
        if not tag:
            return
        if start.has_tag(tag):
            self._ibuf.remove_tag(tag, start, end)
        else:
            self._ibuf.apply_tag(tag, start, end)

    def _clear_selection_tags(self) -> None:
        sel = self._ibuf.get_selection_bounds()
        if not sel:
            return
        start, end = sel
        for tag_name in _TAG_TO_IRC:
            tag = self._ibuf.get_tag_table().lookup(tag_name)
            if tag:
                self._ibuf.remove_tag(tag, start, end)

    def _serialize(
        self, start: Gtk.TextIter, end: Gtk.TextIter,
    ) -> str:
        """Walk the buffer and emit IRC control codes at tag boundaries."""
        result: list[str] = []
        it = start.copy()
        prev_tags: set[str] = set()

        while it.compare(end) < 0:
            cur_tags: set[str] = set()
            for tag in it.get_tags():
                name = tag.props.name
                if name and name in _TAG_TO_IRC:
                    cur_tags.add(name)

            turned_off = prev_tags - cur_tags
            turned_on = cur_tags - prev_tags

            if turned_off or turned_on:
                for tag_name in turned_off:
                    result.append(_TAG_TO_IRC[tag_name])
                for tag_name in turned_on:
                    result.append(_TAG_TO_IRC[tag_name])

            result.append(it.get_char())
            prev_tags = cur_tags
            it.forward_char()

        if prev_tags:
            result.append("\x0F")

        return "".join(result)


_PAGE_SIZE = 100


# -- Main window --------------------------------------------------------------

class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app: Lip2App) -> None:
        super().__init__(application=app, title="Lip2")
        self._app = app
        self.set_default_size(900, 600)

        self._current_network: str | None = None
        self._current_channel: str | None = None
        self._current_query: str | None = None
        self._last_msg_id: str | None = None
        self._oldest_msg_id: str | None = None
        self._has_more: bool = False
        self._loading_more: bool = False
        self._msg_count: int = 0
        self._last_date: str | None = None
        self._prepend_date: str | None = None
        self._insert_mark: Gtk.TextMark | None = None
        self._network_rows: dict[str, SidebarRow] = {}
        self._channel_rows: dict[tuple[str, str], SidebarRow] = {}
        self._query_rows: dict[tuple[str, str], SidebarRow] = {}
        self._pointers: dict[str, str] = {}
        self._nicks: dict[str, str] = {}
        self._sse_running = False

        self._build_ui()
        self._restore_session()

    def _build_ui(self) -> None:
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_position(200)
        paned.set_shrink_start_child(False)

        # -- sidebar --
        sidebar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sidebar_box.set_size_request(180, -1)

        sw = Gtk.ScrolledWindow()
        sw.set_vexpand(True)
        self._sidebar = Gtk.ListBox()
        self._sidebar.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._sidebar.connect("row-selected", self._on_row_selected)

        placeholder = Gtk.Label(label="Right-click to add a network")
        placeholder.set_opacity(0.5)
        placeholder.set_vexpand(True)
        placeholder.set_valign(Gtk.Align.CENTER)
        self._sidebar.set_placeholder(placeholder)

        self._popover: Gtk.Popover | None = None
        click = Gtk.GestureClick(button=3)
        click.connect("pressed", self._on_sidebar_right_click)
        self._sidebar.add_controller(click)
        sw.set_child(self._sidebar)
        sidebar_box.append(sw)

        paned.set_start_child(sidebar_box)

        # -- right pane --
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        self._header = Gtk.Label(label="No channel selected")
        self._header.set_xalign(0)
        self._header.set_margin_start(8)
        self._header.set_margin_top(6)
        self._header.set_margin_bottom(6)
        right.append(self._header)
        right.append(Gtk.Separator())

        self._msg_sw = Gtk.ScrolledWindow()
        self._msg_sw.set_vexpand(True)

        self._msg_view = Gtk.TextView()
        self._msg_view.set_editable(False)
        self._msg_view.set_cursor_visible(False)
        self._msg_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._msg_view.set_left_margin(8)
        self._msg_view.set_right_margin(8)
        self._msg_view.set_top_margin(4)
        self._msg_view.set_bottom_margin(4)
        ctx_gesture = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        ctx_gesture.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        ctx_gesture.connect("pressed", self._on_msg_context)
        self._msg_view.add_controller(ctx_gesture)

        self._buf = self._msg_view.get_buffer()
        self._buf.set_enable_undo(False)
        self._buf.create_tag("nick", weight=Pango.Weight.BOLD)
        self._buf.create_tag(
            "meta", style=Pango.Style.ITALIC, foreground="#888888",
        )
        self._buf.create_tag("time", foreground="#888888", scale=0.9)
        self._buf.create_tag("action", style=Pango.Style.ITALIC)
        self._buf.create_tag("irc_bold", weight=Pango.Weight.BOLD)
        self._buf.create_tag("irc_italic", style=Pango.Style.ITALIC)
        self._buf.create_tag("irc_underline", underline=Pango.Underline.SINGLE)
        self._buf.create_tag("irc_strike", strikethrough=True)
        self._buf.create_tag("irc_mono", family="Monospace")
        self._buf.create_tag(
            "mention", weight=Pango.Weight.BOLD, background="#fce4b8",
        )
        self._buf.create_tag(
            "link", foreground="#1a0dab", underline=Pango.Underline.SINGLE,
        )
        self._buf.create_tag("search_match", background="#ffe08a")
        self._buf.create_tag(
            "search_current", background="#f5c211",
            weight=Pango.Weight.BOLD,
        )

        click_ctrl = Gtk.GestureClick()
        click_ctrl.connect("released", self._on_msg_click)
        self._msg_view.add_controller(click_ctrl)
        motion_ctrl = Gtk.EventControllerMotion()
        motion_ctrl.connect("motion", self._on_msg_motion)
        self._msg_view.add_controller(motion_ctrl)
        self._hand_cursor = Gdk.Cursor.new_from_name("pointer")
        self._text_cursor = Gdk.Cursor.new_from_name("text")

        self._msg_sw.set_child(self._msg_view)
        self._vadj = self._msg_sw.get_vadjustment()
        self._vadj.connect("value-changed", self._on_scroll)
        right.append(self._msg_sw)

        self._search_bar = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=4,
        )
        self._search_bar.set_margin_start(4)
        self._search_bar.set_margin_end(4)
        self._search_bar.set_margin_top(2)
        self._search_bar.set_margin_bottom(2)
        self._search_entry = Gtk.Entry()
        self._search_entry.set_placeholder_text("Search\u2026")
        self._search_entry.set_hexpand(True)
        self._search_entry.connect("activate", self._on_search_next)
        self._search_bar.append(self._search_entry)
        prev_btn = Gtk.Button(label="\u25b2")
        prev_btn.set_tooltip_text("Previous (Shift+Enter)")
        prev_btn.connect("clicked", self._on_search_prev)
        self._search_bar.append(prev_btn)
        next_btn = Gtk.Button(label="\u25bc")
        next_btn.set_tooltip_text("Next (Enter)")
        next_btn.connect("clicked", self._on_search_next)
        self._search_bar.append(next_btn)
        close_btn = Gtk.Button(label="\u2715")
        close_btn.connect("clicked", self._on_search_close)
        self._search_bar.append(close_btn)
        self._search_bar.set_visible(False)
        right.append(self._search_bar)

        search_key = Gtk.EventControllerKey()
        search_key.connect("key-pressed", self._on_search_key)
        self._search_entry.add_controller(search_key)

        self._search_query: str = ""
        self._search_match_id: str | None = None
        self._search_positions: list[tuple[int, int]] = []
        self._search_idx: int = -1

        input_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=4,
        )
        input_box.set_margin_start(4)
        input_box.set_margin_end(4)
        input_box.set_margin_top(4)
        input_box.set_margin_bottom(4)

        self._input = FormattedInput()
        self._input.set_hexpand(True)
        self._input.connect("send", self._on_send)
        self._input.set_sensitive(False)
        self._input.night_btn.connect("toggled", self._on_night_toggled)
        input_box.append(self._input)

        right.append(input_box)
        paned.set_end_child(right)
        self.set_child(paned)

        win_key = Gtk.EventControllerKey()
        win_key.connect("key-pressed", self._on_win_key)
        self.add_controller(win_key)

        for name, cb in [
            ("copy", lambda *_: self._msg_view.emit("copy-clipboard")),
            ("select-all", lambda *_: self._buf.select_range(
                self._buf.get_start_iter(), self._buf.get_end_iter(),
            )),
            ("search", lambda *_: self._toggle_search()),
        ]:
            action = Gio.SimpleAction(name=name)
            action.connect("activate", cb)
            self.add_action(action)

        self._ctx_menu = Gio.Menu()
        self._ctx_menu.append("Copy", "win.copy")
        self._ctx_menu.append("Select All", "win.select-all")
        self._ctx_menu.append("Search\u2026", "win.search")

        dark = _load_config().get("dark") == "true"
        if dark:
            self._input.night_btn.set_active(True)
        self._apply_color_scheme(dark)

    # -- session management ---------------------------------------------------

    def _restore_session(self) -> None:
        def fetch() -> dict[str, Any]:
            try:
                return self._app.api.get_session()
            except Exception:
                return {}

        def apply(data: dict[str, Any]) -> None:
            self._pointers = data.get("pointers", {})
            self._current_network = data.get("current_network")
            self._current_channel = data.get("current_channel")
            self._current_query = data.get("current_query")
            self._load_sidebar()
            self._start_sse()

        _run_in_thread(fetch, apply)

    def _save_session(self) -> None:
        data: dict[str, Any] = {
            "current_network": self._current_network,
            "current_channel": self._current_channel,
            "current_query": self._current_query,
            "pointers": self._pointers,
        }

        def save() -> None:
            try:
                self._app.api.save_session(data)
            except Exception:
                pass

        _run_in_thread(save)

    def _update_pointer(self) -> None:
        if not self._current_network or not self._last_msg_id:
            return
        target = self._current_channel or self._current_query
        if target:
            key = f"{self._current_network}/{target}"
            self._pointers[key] = self._last_msg_id

    # -- sidebar loading ------------------------------------------------------

    def _load_sidebar(self) -> None:
        saved_net = self._current_network
        saved_ch = self._current_channel
        saved_q = self._current_query

        def fetch() -> list[dict[str, Any]]:
            api = self._app.api
            networks = api.list_networks()
            for net in networks:
                net["_channels"] = api.list_channels(net["name"])
                net["_queries"] = api.list_queries(net["name"])
            return networks

        def populate(networks: list[dict[str, Any]]) -> None:
            self._clear_sidebar()
            self._network_rows.clear()
            self._channel_rows.clear()
            self._query_rows.clear()
            self._nicks = {
                net["name"]: net["nick"]
                for net in networks if net.get("nick")
            }
            reselect: SidebarRow | None = None
            first_selectable: SidebarRow | None = None
            for net in networks:
                net_row = SidebarRow(net["name"], state=net["state"])
                self._sidebar.append(net_row)
                self._network_rows[net["name"]] = net_row
                for ch in net["_channels"]:
                    if not ch.get("joined", True):
                        continue
                    row = SidebarRow(net["name"], channel=ch["name"])
                    self._sidebar.append(row)
                    self._channel_rows[(net["name"], ch["name"])] = row
                    if first_selectable is None:
                        first_selectable = row
                    if (net["name"] == saved_net
                            and ch["name"] == saved_ch):
                        reselect = row
                for q in net["_queries"]:
                    nick = q["nick"]
                    row = SidebarRow(net["name"], query=nick)
                    self._sidebar.append(row)
                    self._query_rows[(net["name"], nick)] = row
                    if first_selectable is None:
                        first_selectable = row
                    if (net["name"] == saved_net
                            and nick == saved_q):
                        reselect = row
            if networks and not first_selectable:
                self._sidebar.append(self._hint_row(
                    "Right-click to join a channel",
                ))
            pick = reselect or first_selectable
            if pick:
                self._sidebar.select_row(pick)
            else:
                self._show_empty_hint()

        _run_in_thread(fetch, populate, lambda e: self._show_error(str(e)))

    @staticmethod
    def _hint_row(text: str) -> Gtk.ListBoxRow:
        label = Gtk.Label(label=text)
        label.set_opacity(0.5)
        label.set_margin_top(16)
        label.set_margin_bottom(16)
        row = Gtk.ListBoxRow()
        row.set_child(label)
        row.set_selectable(False)
        row.set_activatable(False)
        return row

    def _clear_sidebar(self) -> None:
        while True:
            row = self._sidebar.get_row_at_index(0)
            if row is None:
                break
            self._sidebar.remove(row)

    def _add_query_row(
        self, network: str, nick: str, unread: bool = False,
    ) -> SidebarRow:
        row = SidebarRow(network, query=nick)
        self._sidebar.append(row)
        self._query_rows[(network, nick)] = row
        if unread:
            row.set_unread(True)
        return row

    # -- channel selection & messages -----------------------------------------

    def _on_row_selected(
        self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow | None,
    ) -> None:
        if not row or not isinstance(row, SidebarRow):
            return
        if not row.is_selectable_target:
            return
        self._update_pointer()
        self._current_network = row.network
        if row.channel:
            self._current_channel = row.channel
            self._current_query = None
            self._header.set_text(f"{row.network} / {row.channel}")
        elif row.query:
            self._current_channel = None
            self._current_query = row.query
            self._header.set_text(f"{row.network} / {row.query}")
        row.set_unread(False)
        self._update_title_badge()
        self._input.set_sensitive(True)
        if self._search_bar.get_visible():
            self._close_search()
        self._input.grab_focus()
        self._load_messages()
        self._save_session()

    def _load_messages(self) -> None:
        net = self._current_network
        ch = self._current_channel
        q = self._current_query
        if not net or (not ch and not q):
            return

        def fetch() -> dict[str, Any]:
            if ch:
                return self._app.api.list_messages(
                    net, ch, limit=_PAGE_SIZE,
                )
            return self._app.api.list_private_messages(
                net, q, limit=_PAGE_SIZE,
            )

        def display(data: dict[str, Any]) -> None:
            if self._current_network != net:
                return
            if ch and self._current_channel != ch:
                return
            if q and self._current_query != q:
                return
            self._buf.set_text("")
            self._last_date = None
            self._prepend_date = None
            self._msg_count = 0
            messages = data.get("messages", [])
            for msg in messages:
                self._append_message(msg)
                self._msg_count += 1
            if messages:
                self._last_msg_id = messages[-1]["id"]
                self._oldest_msg_id = messages[0]["id"]
                self._prepend_date = self._local_date(
                    messages[0].get("time", ""),
                )
            else:
                self._last_msg_id = None
                self._oldest_msg_id = None
            self._has_more = data.get("has_more", False)
            self._loading_more = False
            self._scroll_to_bottom()

        _run_in_thread(fetch, display, lambda e: self._show_error(str(e)))

    def _ipt(self) -> Gtk.TextIter:
        """Return the current insertion point (mark-aware)."""
        if self._insert_mark:
            return self._buf.get_iter_at_mark(self._insert_mark)
        return self._buf.get_end_iter()

    def _insert_tagged(
        self, text: str, *tags: Gtk.TextTag,
    ) -> None:
        """Insert text at _ipt, stripping inherited tags first."""
        offset = self._ipt().get_offset()
        self._buf.insert(self._ipt(), text)
        start = self._buf.get_iter_at_offset(offset)
        end = self._buf.get_iter_at_offset(offset + len(text))
        self._buf.remove_all_tags(start, end)
        for tag in tags:
            self._buf.apply_tag(tag, start, end)

    def _insert_tagged_by_name(
        self, text: str, *tag_names: str,
    ) -> None:
        """Insert text at _ipt with named tags, stripping inherited tags."""
        table = self._buf.get_tag_table()
        tags = [t for n in tag_names if (t := table.lookup(n))]
        self._insert_tagged(text, *tags)

    def _render_msg(self, msg: dict[str, Any]) -> None:
        """Render a message line at the current insertion point."""
        time_str = self._format_time(msg.get("time", ""))
        self._insert_tagged_by_name(f"[{time_str}] ", "time")
        msg_type = msg.get("type", "privmsg")
        sender = msg.get("from", "")
        if msg_type == "meta":
            self._insert_tagged_by_name(
                f"\u2014 {msg['text']} \u2014", "meta",
            )
        elif msg_type == "action":
            self._insert_tagged_by_name(f"* {sender} ", "action")
            self._insert_irc_formatted(msg["text"], base_tags=["action"])
        elif msg_type == "notice":
            self._insert_tagged_by_name(f"-{sender}- ", "nick")
            self._insert_irc_formatted(msg["text"])
        else:
            self._insert_tagged_by_name(f"<{sender}> ", "nick")
            self._insert_irc_formatted(msg["text"])

    def _append_message(self, msg: dict[str, Any]) -> None:
        date_str = self._local_date(msg.get("time", ""))
        if date_str and date_str != self._last_date:
            self._last_date = date_str
            if self._buf.get_char_count() > 0:
                self._insert_tagged("\n")
            self._insert_tagged_by_name(
                f"\u2014 {date_str} \u2014", "meta",
            )
        if self._buf.get_char_count() > 0:
            self._insert_tagged("\n")
        self._render_msg(msg)

    def _get_color_tag(
        self, color: str, prop: str,
    ) -> Gtk.TextTag:
        name = f"irc_{prop}_{color}"
        tag = self._buf.get_tag_table().lookup(name)
        if tag is None:
            if prop == "fg":
                tag = self._buf.create_tag(name, foreground=color)
            else:
                tag = self._buf.create_tag(name, background=color)
        return tag

    def _mention_re(self) -> re.Pattern[str] | None:
        net = self._current_network
        if not net:
            return None
        nick = self._nicks.get(net)
        if not nick:
            return None
        escaped = re.escape(nick)
        nick_char = r"[A-Za-z0-9\[\]\\`_^{|}\-]"
        return re.compile(
            rf"(?<!{nick_char}){escaped}(?!{nick_char})", re.IGNORECASE,
        )

    def _insert_irc_formatted(
        self, text: str, base_tags: list[str] | None = None,
    ) -> None:
        tag_table = self._buf.get_tag_table()
        mention_re = self._mention_re()
        mention_tag = tag_table.lookup("mention")
        style_map = {
            "bold": "irc_bold", "italic": "irc_italic",
            "underline": "irc_underline", "strikethrough": "irc_strike",
            "monospace": "irc_mono",
        }
        for span_text, styles in parse_irc(text):
            tags: list[Gtk.TextTag] = []
            if base_tags:
                for bt in base_tags:
                    tag = tag_table.lookup(bt)
                    if tag:
                        tags.append(tag)
            for key, tag_name in style_map.items():
                if styles.get(key):
                    tag = tag_table.lookup(tag_name)
                    if tag:
                        tags.append(tag)
            fg = styles.get("fg")
            if isinstance(fg, str):
                tags.append(self._get_color_tag(fg, "fg"))
            bg = styles.get("bg")
            if isinstance(bg, str):
                tags.append(self._get_color_tag(bg, "bg"))
            self._insert_rich(span_text, tags, mention_re, mention_tag)

    def _insert_rich(
        self, text: str, tags: list[Gtk.TextTag],
        mention_re: re.Pattern[str] | None,
        mention_tag: Gtk.TextTag | None,
    ) -> None:
        """Insert text, highlighting URLs and nick mentions."""
        link_tag = self._buf.get_tag_table().lookup("link")
        regions: list[tuple[int, int, Gtk.TextTag]] = []
        if link_tag:
            for m in _URL_RE.finditer(text):
                regions.append((m.start(), m.end(), link_tag))
        if mention_re and mention_tag:
            for m in mention_re.finditer(text):
                regions.append((m.start(), m.end(), mention_tag))
        if not regions:
            self._insert_tagged(text, *tags)
            return
        regions.sort(key=lambda r: r[0])
        merged: list[tuple[int, int, Gtk.TextTag]] = []
        for r in regions:
            if merged and r[0] < merged[-1][1]:
                continue
            merged.append(r)
        pos = 0
        for start, end_pos, extra_tag in merged:
            if start > pos:
                self._insert_tagged(text[pos:start], *tags)
            self._insert_tagged(text[start:end_pos], *tags, extra_tag)
            pos = end_pos
        if pos < len(text):
            self._insert_tagged(text[pos:], *tags)

    def _iter_at_xy(self, x: float, y: float) -> Gtk.TextIter | None:
        bx, by = self._msg_view.window_to_buffer_coords(
            Gtk.TextWindowType.WIDGET, int(x), int(y),
        )
        ok, it = self._msg_view.get_iter_at_location(bx, by)
        return it if ok else None

    def _url_at_iter(self, it: Gtk.TextIter) -> str | None:
        link_tag = self._buf.get_tag_table().lookup("link")
        if not link_tag or not it.has_tag(link_tag):
            return None
        start = it.copy()
        start.backward_to_tag_toggle(link_tag)
        end = it.copy()
        end.forward_to_tag_toggle(link_tag)
        return self._buf.get_text(start, end, False)

    def _on_msg_context(
        self, gesture: Gtk.GestureClick,
        _n_press: int, x: float, y: float,
    ) -> None:
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._dismiss_popover()
        popover = Gtk.PopoverMenu(menu_model=self._ctx_menu)
        popover.set_parent(self._msg_view)
        rect = Gdk.Rectangle()
        rect.x, rect.y = int(x), int(y)
        rect.width = rect.height = 1
        popover.set_pointing_to(rect)
        popover.set_has_arrow(True)
        popover.connect("closed", lambda *_: self._dismiss_popover())
        self._popover = popover
        popover.popup()

    def _on_msg_click(
        self, _gesture: Gtk.GestureClick,
        _n_press: int, x: float, y: float,
    ) -> None:
        it = self._iter_at_xy(x, y)
        if not it:
            return
        url = self._url_at_iter(it)
        if url:
            Gtk.show_uri(self, url, 0)

    def _on_msg_motion(
        self, _ctrl: Gtk.EventControllerMotion,
        x: float, y: float,
    ) -> None:
        it = self._iter_at_xy(x, y)
        if it and self._url_at_iter(it):
            self._msg_view.set_cursor(self._hand_cursor)
        else:
            self._msg_view.set_cursor(self._text_cursor)

    @staticmethod
    def _format_time(iso: str) -> str:
        if not iso:
            return "??:??"
        try:
            dt = datetime.fromisoformat(iso).astimezone()
            return dt.strftime("%H:%M")
        except ValueError:
            return "??:??"

    @staticmethod
    def _local_date(iso: str) -> str | None:
        if not iso:
            return None
        try:
            dt = datetime.fromisoformat(iso).astimezone()
            return dt.strftime("%A, %B %-d, %Y")
        except ValueError:
            return None

    def _scroll_to_bottom(self) -> None:
        def scroll() -> bool:
            self._vadj.set_value(
                self._vadj.get_upper() - self._vadj.get_page_size(),
            )
            return False
        GLib.idle_add(scroll)

    def _is_near_bottom(self) -> bool:
        return (self._vadj.get_value()
                >= self._vadj.get_upper()
                - self._vadj.get_page_size() - 50)

    def _on_scroll(self, adj: Gtk.Adjustment) -> None:
        if adj.get_upper() <= adj.get_page_size():
            return
        if adj.get_value() <= adj.get_page_size() * 0.5:
            self._load_older()

    def _load_older(self) -> None:
        if self._loading_more or not self._has_more:
            return
        if not self._oldest_msg_id:
            return
        net = self._current_network
        ch = self._current_channel
        q = self._current_query
        if not net or (not ch and not q):
            return
        self._loading_more = True
        oldest = self._oldest_msg_id

        def fetch() -> dict[str, Any]:
            if ch:
                return self._app.api.list_messages(
                    net, ch, limit=_PAGE_SIZE, before=oldest,
                )
            return self._app.api.list_private_messages(
                net, q, limit=_PAGE_SIZE, before=oldest,
            )

        def prepend(data: dict[str, Any]) -> None:
            if self._current_network != net:
                return
            if ch and self._current_channel != ch:
                return
            if q and self._current_query != q:
                return
            messages = data.get("messages", [])
            self._has_more = data.get("has_more", False)
            if not messages:
                self._loading_more = False
                return
            self._oldest_msg_id = messages[0]["id"]
            old_upper = self._vadj.get_upper()
            self._strip_top_date_sep()
            for msg in reversed(messages):
                self._prepend_message(msg)
                self._msg_count += 1
            self._insert_top_date_sep()

            def restore() -> bool:
                delta = self._vadj.get_upper() - old_upper
                self._vadj.set_value(self._vadj.get_value() + delta)
                self._loading_more = False
                return False
            GLib.idle_add(restore)

        def on_err(e: Exception) -> None:
            self._loading_more = False
            self._show_error(str(e))

        _run_in_thread(fetch, prepend, on_err)

    def _strip_top_date_sep(self) -> None:
        """Remove a date separator line at the top of the buffer."""
        start = self._buf.get_start_iter()
        if start.is_end():
            return
        line_end = start.copy()
        line_end.forward_to_line_end()
        first_line = self._buf.get_text(start, line_end, False)
        if first_line.startswith("\u2014") and first_line.endswith("\u2014"):
            delete_end = line_end.copy()
            delete_end.forward_char()
            self._buf.delete(start, delete_end)

    def _insert_top_date_sep(self) -> None:
        """Insert a date separator at the very top for _prepend_date."""
        if not self._prepend_date:
            return
        mark = self._buf.create_mark(
            None, self._buf.get_start_iter(), False,
        )
        self._insert_mark = mark
        self._insert_tagged_by_name(
            f"\u2014 {self._prepend_date} \u2014", "meta",
        )
        self._insert_tagged("\n")
        self._insert_mark = None
        self._buf.delete_mark(mark)

    def _prepend_message(self, msg: dict[str, Any]) -> None:
        """Insert a message at the very top of the buffer."""
        had_content = self._buf.get_char_count() > 0
        mark = self._buf.create_mark(
            None, self._buf.get_start_iter(), False,
        )
        self._insert_mark = mark
        date_str = self._local_date(msg.get("time", ""))
        self._render_msg(msg)
        if had_content:
            if (date_str and self._prepend_date
                    and date_str != self._prepend_date):
                self._insert_tagged("\n")
                self._insert_tagged_by_name(
                    f"\u2014 {self._prepend_date} \u2014", "meta",
                )
            self._insert_tagged("\n")
        if date_str:
            self._prepend_date = date_str
        self._insert_mark = None
        self._buf.delete_mark(mark)

    def _update_title_badge(self) -> None:
        has_unread = any(
            r._unread for r in self._channel_rows.values()
        ) or any(
            r._unread for r in self._query_rows.values()
        )
        self.set_title("* Lip2" if has_unread else "Lip2")

    # -- night mode -----------------------------------------------------------

    def _apply_color_scheme(self, dark: bool) -> None:
        settings = Gtk.Settings.get_default()
        if settings:
            settings.set_property(
                "gtk-application-prefer-dark-theme", dark,
            )
        self._input.night_btn.set_label("\u2600" if dark else "\u263e")
        tag_table = self._buf.get_tag_table()
        mention = tag_table.lookup("mention")
        if mention:
            mention.set_property(
                "background", "#5a4a20" if dark else "#fce4b8",
            )
        link = tag_table.lookup("link")
        if link:
            link.set_property(
                "foreground", "#6ea8fe" if dark else "#1a0dab",
            )
        search_match = tag_table.lookup("search_match")
        if search_match:
            search_match.set_property(
                "background", "#5a4a00" if dark else "#ffe08a",
            )
        search_current = tag_table.lookup("search_current")
        if search_current:
            search_current.set_property(
                "background", "#8a6d00" if dark else "#f5c211",
            )

    def _on_night_toggled(self, btn: Gtk.ToggleButton) -> None:
        dark = btn.get_active()
        self._apply_color_scheme(dark)
        cfg = _load_config()
        cfg["dark"] = "true" if dark else "false"
        _save_config_dict(cfg)

    # -- sending messages -----------------------------------------------------

    def _on_send(self, _widget: Gtk.Widget) -> None:
        text = self._input.get_irc_text().strip(" \t\n\r")
        net = self._current_network
        ch = self._current_channel
        q = self._current_query
        if not text or not net or (not ch and not q):
            return
        self._input.clear()

        msg_type = "privmsg"
        if text.startswith("/me "):
            msg_type = "action"
            text = text[4:]

        def send() -> None:
            if ch:
                self._app.api.send_message(net, ch, text, msg_type)
            else:
                self._app.api.send_private_message(net, q, text, msg_type)

        _run_in_thread(send, None, lambda e: self._show_error(str(e)))

    # -- search ---------------------------------------------------------------

    def _on_win_key(
        self, _ctrl: Gtk.EventControllerKey,
        keyval: int, _keycode: int, state: Gdk.ModifierType,
    ) -> bool:
        if (keyval == Gdk.KEY_f
                and state & Gdk.ModifierType.CONTROL_MASK):
            self._toggle_search()
            return True
        if keyval == Gdk.KEY_Escape and self._search_bar.get_visible():
            self._close_search()
            return True
        return False

    def _on_search_key(
        self, _ctrl: Gtk.EventControllerKey,
        keyval: int, _keycode: int, state: Gdk.ModifierType,
    ) -> bool:
        if keyval == Gdk.KEY_Escape:
            self._close_search()
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if state & Gdk.ModifierType.SHIFT_MASK:
                self._on_search_prev()
            else:
                self._on_search_next()
            return True
        return False

    def _toggle_search(self) -> None:
        if self._search_bar.get_visible():
            self._close_search()
        else:
            self._search_bar.set_visible(True)
            self._search_entry.grab_focus()

    def _close_search(self) -> None:
        self._search_bar.set_visible(False)
        self._clear_search_highlights()
        self._search_query = ""
        self._search_match_id = None
        self._input.grab_focus()

    def _on_search_close(self, *_args: Any) -> None:
        self._close_search()

    def _on_search_next(self, *_args: Any) -> None:
        self._do_search("forward")

    def _on_search_prev(self, *_args: Any) -> None:
        self._do_search("backward")

    def _do_search(self, direction: str) -> None:
        query = self._search_entry.get_text().strip()
        if not query:
            return
        new_query = query != self._search_query
        if new_query:
            self._search_query = query
            self._search_match_id = None
            self._rescan_matches(query)
            if self._search_positions:
                self._search_idx = len(self._search_positions) - 1
                self._show_current_match()
                return
        elif self._navigate_local(direction):
            return
        self._search_server(query, direction)

    def _rescan_matches(self, query: str) -> None:
        self._clear_search_highlights()
        self._search_positions.clear()
        it = self._buf.get_start_iter()
        while True:
            result = it.forward_search(
                query, Gtk.TextSearchFlags.CASE_INSENSITIVE, None,
            )
            if not result:
                break
            ms, me = result
            self._buf.apply_tag_by_name("search_match", ms, me)
            self._search_positions.append(
                (ms.get_offset(), me.get_offset()),
            )
            it = me

    def _navigate_local(self, direction: str) -> bool:
        if not self._search_positions:
            return False
        if direction == "backward":
            new_idx = self._search_idx - 1
        else:
            new_idx = self._search_idx + 1
        if 0 <= new_idx < len(self._search_positions):
            self._search_idx = new_idx
            self._show_current_match()
            return True
        return False

    def _show_current_match(self) -> None:
        if not (0 <= self._search_idx < len(self._search_positions)):
            return
        start = self._buf.get_start_iter()
        end = self._buf.get_end_iter()
        self._buf.remove_tag_by_name("search_current", start, end)
        s_off, e_off = self._search_positions[self._search_idx]
        ms = self._buf.get_iter_at_offset(s_off)
        me = self._buf.get_iter_at_offset(e_off)
        self._buf.apply_tag_by_name("search_current", ms, me)
        mark = self._buf.create_mark(None, ms, True)
        self._msg_view.scroll_to_mark(mark, 0.1, True, 0.0, 0.5)
        self._buf.delete_mark(mark)

    def _search_server(self, query: str, direction: str) -> None:
        net = self._current_network
        ch = self._current_channel
        q = self._current_query
        if not net or (not ch and not q):
            return
        anchor = self._search_match_id

        def fetch() -> dict[str, Any]:
            if ch:
                return self._app.api.search_messages(
                    net, ch, query, anchor=anchor, direction=direction,
                )
            return self._app.api.search_private_messages(
                net, q, query, anchor=anchor, direction=direction,
            )

        def show(data: dict[str, Any]) -> None:
            msgs = data.get("messages", [])
            if not msgs:
                return
            match_msg = msgs[0]
            self._search_match_id = match_msg["id"]
            if self._msg_in_buffer(match_msg["id"]):
                self._rescan_matches(query)
                self._jump_to_nearest(match_msg["id"], direction)
            else:
                self._load_around(match_msg["id"], query, direction)

        _run_in_thread(fetch, show, lambda e: self._show_error(str(e)))

    def _msg_in_buffer(self, msg_id: str) -> bool:
        oldest = self._oldest_msg_id
        newest = self._last_msg_id
        if not oldest or not newest:
            return False
        return oldest <= msg_id <= newest

    def _jump_to_nearest(
        self, msg_id: str, direction: str,
    ) -> None:
        """Set search_idx to the match nearest to msg_id's position."""
        if direction == "backward":
            self._search_idx = len(self._search_positions) - 1
        else:
            self._search_idx = 0
        self._show_current_match()

    def _load_around(
        self, msg_id: str, query: str, direction: str,
    ) -> None:
        net = self._current_network
        ch = self._current_channel
        q = self._current_query
        if not net or (not ch and not q):
            return

        def fetch() -> dict[str, Any]:
            if ch:
                return self._app.api.list_messages(
                    net, ch, limit=_PAGE_SIZE, around=msg_id,
                )
            return self._app.api.list_private_messages(
                net, q, limit=_PAGE_SIZE, around=msg_id,
            )

        def display(data: dict[str, Any]) -> None:
            if self._current_network != net:
                return
            if ch and self._current_channel != ch:
                return
            if q and self._current_query != q:
                return
            self._buf.set_text("")
            self._last_date = None
            self._prepend_date = None
            self._msg_count = 0
            messages = data.get("messages", [])
            for msg in messages:
                self._append_message(msg)
                self._msg_count += 1
            if messages:
                self._last_msg_id = messages[-1]["id"]
                self._oldest_msg_id = messages[0]["id"]
                self._prepend_date = self._local_date(
                    messages[0].get("time", ""),
                )
            else:
                self._last_msg_id = None
                self._oldest_msg_id = None
            self._has_more = data.get("has_more", False)
            self._loading_more = False

            def highlight() -> bool:
                self._rescan_matches(query)
                self._jump_to_nearest(msg_id, direction)
                return False
            GLib.idle_add(highlight)

        _run_in_thread(fetch, display, lambda e: self._show_error(str(e)))

    def _clear_search_highlights(self) -> None:
        start = self._buf.get_start_iter()
        end = self._buf.get_end_iter()
        self._buf.remove_tag_by_name("search_match", start, end)
        self._buf.remove_tag_by_name("search_current", start, end)

    # -- SSE event stream -----------------------------------------------------

    def _start_sse(self) -> None:
        self._sse_running = True
        threading.Thread(target=self._sse_loop, daemon=True).start()

    def _sse_loop(self) -> None:
        delay = 1.0
        while self._sse_running:
            try:
                for event in self._app.api.event_stream():
                    if not self._sse_running:
                        return
                    GLib.idle_add(self._handle_sse, event)
                    delay = 1.0
            except Exception:
                if not self._sse_running:
                    return
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
            if self._sse_running:
                GLib.idle_add(self._load_sidebar)

    def _handle_sse(self, event: dict[str, Any]) -> bool:
        ev_type = event["event"]
        data = event["data"]

        if ev_type == "message":
            net = data.get("network")
            ch = data.get("channel")
            nick = data.get("nick")
            if ch:
                if (net == self._current_network
                        and ch == self._current_channel):
                    msg_id = data.get("id", "")
                    if msg_id != self._last_msg_id:
                        was_at_bottom = self._is_near_bottom()
                        self._append_message(data)
                        self._last_msg_id = msg_id
                        if was_at_bottom:
                            self._scroll_to_bottom()
                else:
                    ch_row = self._channel_rows.get((net, ch))
                    if ch_row:
                        ch_row.set_unread(True)
                        self._update_title_badge()
            elif nick:
                if (net == self._current_network
                        and nick == self._current_query):
                    msg_id = data.get("id", "")
                    if msg_id != self._last_msg_id:
                        was_at_bottom = self._is_near_bottom()
                        self._append_message(data)
                        self._last_msg_id = msg_id
                        if was_at_bottom:
                            self._scroll_to_bottom()
                else:
                    q_row = self._query_rows.get((net, nick))
                    if q_row:
                        q_row.set_unread(True)
                    elif net:
                        self._add_query_row(net, nick, unread=True)
                    self._update_title_badge()

        elif ev_type == "network_state":
            net_name = data.get("network", "")
            state = data.get("state", "")
            row = self._network_rows.get(net_name)
            if row:
                row.update(state)
            if net_name == self._current_network:
                meta_text = {
                    "connected": f"Connected to {net_name}",
                    "disconnected": f"Disconnected from {net_name}",
                    "connecting": f"Connecting to {net_name}...",
                }.get(state)
                if meta_text:
                    self._append_message({
                        "time": datetime.now().astimezone().isoformat(),
                        "from": "", "type": "meta", "text": meta_text,
                    })
                    self._scroll_to_bottom()
            if state == "connected":
                self._load_sidebar()

        elif ev_type == "nick":
            net_name = data.get("network", "")
            old = data.get("old_nick", "")
            new = data.get("new_nick", "")
            if net_name and self._nicks.get(net_name) == old:
                self._nicks[net_name] = new

        elif ev_type in ("join", "part", "kick"):
            self._load_sidebar()

        return False

    # -- network management ---------------------------------------------------

    def _on_sidebar_right_click(
        self, gesture: Gtk.GestureClick,
        _n_press: int, x: float, y: float,
    ) -> None:
        row = self._sidebar.get_row_at_y(int(y))
        menu, box = self._make_popover(x, y)

        if isinstance(row, SidebarRow) and row.query:
            net_name = row.network
            nick = row.query
            self._menu_item(box, "Close...", lambda: self._confirm(
                f"Close conversation with {nick}?",
                lambda: (
                    self._clear_view_if_query(net_name, nick),
                    _run_in_thread(
                        lambda: self._app.api.close_query(net_name, nick),
                        lambda _r: self._load_sidebar(),
                        lambda e: self._show_error(str(e)),
                    ),
                ),
            ), menu)
            self._menu_separator(box)

        if isinstance(row, SidebarRow) and row.channel:
            net_name = row.network
            channel = row.channel or ""
            self._menu_item(box, "Leave...", lambda: self._confirm(
                f"Leave {channel}?",
                lambda: (
                    self._clear_view_if_current(net_name, channel),
                    _run_in_thread(
                        lambda: self._app.api.part_channel(net_name, channel),
                        lambda _r: self._load_sidebar(),
                        lambda e: self._show_error(str(e)),
                    ),
                ),
            ), menu)
            self._menu_separator(box)

        if isinstance(row, SidebarRow) and not row.channel:
            net_name = row.network
            state = row.net_state
            if state in ("connected", "connecting"):
                self._menu_item(box, "Disconnect...", lambda: self._confirm(
                    f"Disconnect from {net_name}?",
                    lambda: _run_in_thread(
                        lambda: self._app.api.disconnect_network(net_name),
                        lambda _r: self._load_sidebar(),
                        lambda e: self._show_error(str(e)),
                    ),
                ), menu)
            else:
                self._menu_item(box, "Connect", lambda: _run_in_thread(
                    lambda: self._app.api.connect_network(net_name),
                    lambda _r: self._load_sidebar(),
                    lambda e: self._show_error(str(e)),
                ), menu)
            self._menu_item(box, "Delete...", lambda: self._confirm(
                f"Delete network {net_name}?",
                lambda: _run_in_thread(
                    lambda: self._app.api.delete_network(net_name),
                    lambda _r: self._load_sidebar(),
                    lambda e: self._show_error(str(e)),
                ),
            ), menu)
            self._menu_separator(box)

        has_networks = bool(self._network_rows)
        self._menu_item(
            box, "Join Channel...",
            lambda: self._on_join_clicked(None), menu,
            sensitive=has_networks,
        )
        self._menu_item(
            box, "Private Message...",
            lambda: self._on_start_query_clicked(None), menu,
            sensitive=has_networks,
        )
        self._menu_item(
            box, "Add Network...",
            lambda: self._on_add_network_clicked(None), menu,
        )

        menu.set_child(box)
        menu.popup()

    def _dismiss_popover(self) -> None:
        popover = self._popover
        if popover is not None:
            self._popover = None
            popover.popdown()
            popover.unparent()

    def _make_popover(
        self, x: float, y: float,
    ) -> tuple[Gtk.Popover, Gtk.Box]:
        self._dismiss_popover()
        menu = Gtk.Popover()
        menu.set_parent(self._sidebar)
        menu.connect("closed", lambda _p: self._dismiss_popover())
        self._popover = menu
        r = Gdk.Rectangle()
        r.x = int(x)
        r.y = int(y)
        r.width = 1
        r.height = 1
        menu.set_pointing_to(r)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_top(4)
        box.set_margin_bottom(4)
        box.set_margin_start(4)
        box.set_margin_end(4)
        return menu, box

    @staticmethod
    def _menu_item(
        box: Gtk.Box, label: str,
        action: Callable[[], Any], menu: Gtk.Popover,
        sensitive: bool = True,
    ) -> None:
        btn = Gtk.Button(label=label)
        btn.set_has_frame(False)
        btn.set_sensitive(sensitive)
        def on_click(_b: Gtk.Button) -> None:
            menu.popdown()
            action()
        btn.connect("clicked", on_click)
        box.append(btn)

    @staticmethod
    def _menu_separator(box: Gtk.Box) -> None:
        box.append(Gtk.Separator())

    def _confirm(self, message: str, action: Callable[[], Any]) -> None:
        dialog = Gtk.Window(
            title="Confirm", transient_for=self, modal=True,
        )
        dialog.set_default_size(300, 0)
        dialog.set_resizable(False)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)

        label = Gtk.Label(label=message)
        label.set_wrap(True)
        box.append(label)

        btn_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
        )
        btn_box.set_halign(Gtk.Align.END)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda _: dialog.close())
        btn_box.append(cancel_btn)

        ok_btn = Gtk.Button(label="OK")
        def on_ok(_b: Gtk.Button) -> None:
            dialog.close()
            action()
        ok_btn.connect("clicked", on_ok)
        btn_box.append(ok_btn)

        box.append(btn_box)
        dialog.set_child(box)
        dialog.present()

    def _show_empty_hint(self) -> None:
        self._header.set_text("No channel selected")
        self._current_channel = None
        self._current_query = None
        self._buf.set_text("")
        self._msg_view.grab_focus()
        self._input.set_sensitive(False)

    def _clear_view_if_current(self, network: str, channel: str) -> None:
        if self._current_network == network and self._current_channel == channel:
            self._current_channel = None
            self._show_empty_hint()

    def _clear_view_if_query(self, network: str, nick: str) -> None:
        if self._current_network == network and self._current_query == nick:
            self._current_query = None
            self._show_empty_hint()

    def _on_add_network_clicked(self, _btn: Gtk.Button) -> None:
        dialog = Gtk.Window(
            title="Add Network", transient_for=self, modal=True,
        )
        dialog.set_default_size(360, 0)
        dialog.set_resizable(False)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)

        def field(label: str) -> Gtk.Entry:
            box.append(Gtk.Label(label=label, xalign=0))
            entry = Gtk.Entry()
            box.append(entry)
            return entry

        name_entry = field("Name")
        name_entry.set_placeholder_text("e.g. oftc")
        host_entry = field("Host")
        host_entry.set_placeholder_text("e.g. irc.oftc.net")
        port_entry = field("Port")
        port_entry.set_text("6697")
        nick_entry = field("Nick")

        box.append(Gtk.Label(label="NickServ Password", xalign=0))
        nickserv_entry = Gtk.Entry()
        nickserv_entry.set_visibility(False)
        nickserv_entry.set_placeholder_text("optional")
        box.append(nickserv_entry)

        tls_check = Gtk.CheckButton(label="Use TLS")
        tls_check.set_active(True)
        tls_check.set_margin_top(4)
        box.append(tls_check)

        error_label = Gtk.Label()
        error_label.set_wrap(True)
        error_label.set_visible(False)
        box.append(error_label)

        btn_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
        )
        btn_box.set_halign(Gtk.Align.END)
        btn_box.set_margin_top(8)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda _: dialog.close())
        btn_box.append(cancel_btn)

        add_btn = Gtk.Button(label="Add & Connect")
        btn_box.append(add_btn)
        box.append(btn_box)

        def do_add(_widget: Gtk.Widget) -> None:
            name = name_entry.get_text().strip()
            host = host_entry.get_text().strip()
            nick = nick_entry.get_text().strip()
            try:
                port = int(port_entry.get_text().strip())
            except ValueError:
                error_label.set_text("Port must be a number.")
                error_label.set_visible(True)
                return
            if not name or not host or not nick:
                error_label.set_text("Name, host, and nick are required.")
                error_label.set_visible(True)
                return
            tls = tls_check.get_active()
            ns_pass = nickserv_entry.get_text().strip() or None
            add_btn.set_sensitive(False)
            error_label.set_visible(False)

            def attempt() -> dict[str, Any]:
                self._app.api.create_network(
                    name, host, port, tls, nick,
                    nickserv_password=ns_pass,
                )
                return self._app.api.connect_network(name)

            def on_ok(_result: Any) -> None:
                dialog.close()
                self._load_sidebar()

            def on_err(exc: Exception) -> None:
                add_btn.set_sensitive(True)
                msg = exc.message if isinstance(exc, APIError) else str(exc)
                error_label.set_text(msg)
                error_label.set_visible(True)

            _run_in_thread(attempt, on_ok, on_err)

        add_btn.connect("clicked", do_add)
        for entry in (name_entry, host_entry, port_entry,
                       nick_entry, nickserv_entry):
            entry.connect("activate", do_add)
        dialog.set_child(box)
        dialog.present()

    # -- start query -----------------------------------------------------------

    def _on_start_query_clicked(self, _btn: Gtk.Button) -> None:
        networks = list(self._network_rows.keys())
        if not networks:
            return

        dialog = Gtk.Window(
            title="Private Message", transient_for=self, modal=True,
        )
        dialog.set_default_size(320, 0)
        dialog.set_resizable(False)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)

        box.append(Gtk.Label(label="Network", xalign=0))
        net_combo = Gtk.DropDown.new_from_strings(networks)
        if self._current_network and self._current_network in networks:
            net_combo.set_selected(networks.index(self._current_network))
        box.append(net_combo)

        box.append(Gtk.Label(label="Nick", xalign=0))
        nick_entry = Gtk.Entry()
        nick_entry.set_placeholder_text("nickname")
        box.append(nick_entry)

        btn_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
        )
        btn_box.set_halign(Gtk.Align.END)
        btn_box.set_margin_top(8)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda _: dialog.close())
        btn_box.append(cancel_btn)

        open_btn = Gtk.Button(label="Open")
        btn_box.append(open_btn)
        box.append(btn_box)

        def do_open(_widget: Gtk.Widget) -> None:
            idx = net_combo.get_selected()
            net_name = networks[idx]
            nick = nick_entry.get_text().strip()
            if not nick:
                return
            dialog.close()
            existing = self._query_rows.get((net_name, nick))
            if not existing:
                existing = self._add_query_row(net_name, nick)
            self._sidebar.select_row(existing)

        open_btn.connect("clicked", do_open)
        nick_entry.connect("activate", do_open)

        dialog.set_child(box)
        dialog.present()

    # -- join channel ---------------------------------------------------------

    def _on_join_clicked(self, _btn: Gtk.Button) -> None:
        networks = list(self._network_rows.keys())
        if not networks:
            return

        dialog = Gtk.Window(
            title="Join Channel", transient_for=self, modal=True,
        )
        dialog.set_default_size(320, 0)
        dialog.set_resizable(False)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)

        box.append(Gtk.Label(label="Network", xalign=0))
        net_combo = Gtk.DropDown.new_from_strings(networks)
        if self._current_network and self._current_network in networks:
            net_combo.set_selected(networks.index(self._current_network))
        box.append(net_combo)

        box.append(Gtk.Label(label="Channel", xalign=0))
        chan_entry = Gtk.Entry()
        chan_entry.set_placeholder_text("#channel")
        box.append(chan_entry)

        error_label = Gtk.Label()
        error_label.set_wrap(True)
        error_label.set_visible(False)
        box.append(error_label)

        btn_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
        )
        btn_box.set_halign(Gtk.Align.END)
        btn_box.set_margin_top(8)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda _: dialog.close())
        btn_box.append(cancel_btn)

        join_btn = Gtk.Button(label="Join")
        btn_box.append(join_btn)
        box.append(btn_box)

        def do_join(_widget: Gtk.Widget) -> None:
            idx = net_combo.get_selected()
            net_name = networks[idx]
            channel = chan_entry.get_text().strip()
            if not channel:
                return
            if not channel.startswith(("#", "&", "+", "!")):
                channel = "#" + channel
            join_btn.set_sensitive(False)
            error_label.set_visible(False)

            def attempt() -> dict[str, Any]:
                return self._app.api.join_channel(net_name, channel)

            def on_ok(_result: Any) -> None:
                dialog.close()
                self._load_sidebar()

            def on_err(exc: Exception) -> None:
                join_btn.set_sensitive(True)
                msg = exc.message if isinstance(exc, APIError) else str(exc)
                error_label.set_text(msg)
                error_label.set_visible(True)

            _run_in_thread(attempt, on_ok, on_err)

        join_btn.connect("clicked", do_join)
        chan_entry.connect("activate", do_join)

        dialog.set_child(box)
        dialog.present()

    # -- helpers --------------------------------------------------------------

    def _show_error(self, message: Any) -> None:
        end = self._buf.get_end_iter()
        if self._buf.get_char_count() > 0:
            self._buf.insert(end, "\n")
            end = self._buf.get_end_iter()
        self._buf.insert_with_tags_by_name(end, str(message), "meta")

    def do_close_request(self) -> bool:
        self._sse_running = False
        self._dismiss_popover()
        self._update_pointer()
        self._save_session()
        return False


# -- Application --------------------------------------------------------------

class Lip2App(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(application_id="org.pacujo.lip2")
        self.api: LipserviceAPI | None = None

    def do_activate(self) -> None:
        existing = self.get_active_window()
        if existing:
            existing.present()
            return
        login = LoginWindow(self)
        login.present()

    def open_main_window(self) -> None:
        win = MainWindow(self)
        win.present()
