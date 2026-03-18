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
from gi.repository import Gdk, GLib, GObject, Gtk, Pango  # noqa: E402

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
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        f'url = "{url}"\n',
        f'username = "{username}"\n',
    ]
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

        def attempt() -> str:
            api = LipserviceAPI(url)
            api.login(user, pw)
            return api.token or ""

        def on_ok(token: str) -> None:
            _save_config(url, user)
            self._app.api = LipserviceAPI(url)
            self._app.api.token = token
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
        self._last_date: str | None = None
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

        self._buf = self._msg_view.get_buffer()
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

        self._msg_sw.set_child(self._msg_view)
        right.append(self._msg_sw)

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
        input_box.append(self._input)

        right.append(input_box)
        paned.set_end_child(right)
        self.set_child(paned)

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
        self._input.set_sensitive(True)
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
                return self._app.api.list_messages(net, ch)
            return self._app.api.list_private_messages(net, q)

        def display(data: dict[str, Any]) -> None:
            if self._current_network != net:
                return
            if ch and self._current_channel != ch:
                return
            if q and self._current_query != q:
                return
            self._buf.set_text("")
            self._last_date = None
            messages = data.get("messages", [])
            for msg in messages:
                self._append_message(msg)
            if messages:
                self._last_msg_id = messages[-1]["id"]
            else:
                self._last_msg_id = None
            self._scroll_to_bottom()

        _run_in_thread(fetch, display, lambda e: self._show_error(str(e)))

    def _append_message(self, msg: dict[str, Any]) -> None:
        date_str = self._local_date(msg.get("time", ""))
        if date_str and date_str != self._last_date:
            self._last_date = date_str
            end = self._buf.get_end_iter()
            if self._buf.get_char_count() > 0:
                self._buf.insert(end, "\n")
                end = self._buf.get_end_iter()
            self._buf.insert_with_tags_by_name(
                end, f"\u2014 {date_str} \u2014", "meta",
            )

        end = self._buf.get_end_iter()
        if self._buf.get_char_count() > 0:
            self._buf.insert(end, "\n")
            end = self._buf.get_end_iter()

        time_str = self._format_time(msg.get("time", ""))
        self._buf.insert_with_tags_by_name(
            end, f"[{time_str}] ", "time",
        )
        end = self._buf.get_end_iter()

        msg_type = msg.get("type", "privmsg")
        sender = msg.get("from", "")

        if msg_type == "meta":
            self._buf.insert_with_tags_by_name(
                end, f"\u2014 {msg['text']} \u2014", "meta",
            )
        elif msg_type == "action":
            self._buf.insert_with_tags_by_name(
                end, f"* {sender} ", "action",
            )
            self._insert_irc_formatted(msg["text"], base_tags=["action"])
        elif msg_type == "notice":
            self._buf.insert_with_tags_by_name(
                end, f"-{sender}- ", "nick",
            )
            self._insert_irc_formatted(msg["text"])
        else:
            self._buf.insert_with_tags_by_name(
                end, f"<{sender}> ", "nick",
            )
            self._insert_irc_formatted(msg["text"])

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
            self._insert_with_mentions(
                span_text, tags, mention_re, mention_tag,
            )

    def _insert_with_mentions(
        self, text: str, tags: list[Gtk.TextTag],
        mention_re: re.Pattern[str] | None,
        mention_tag: Gtk.TextTag | None,
    ) -> None:
        if not mention_re or not mention_tag:
            end = self._buf.get_end_iter()
            if tags:
                self._buf.insert_with_tags(end, text, *tags)
            else:
                self._buf.insert(end, text)
            return
        pos = 0
        for m in mention_re.finditer(text):
            if m.start() > pos:
                end = self._buf.get_end_iter()
                chunk = text[pos:m.start()]
                if tags:
                    self._buf.insert_with_tags(end, chunk, *tags)
                else:
                    self._buf.insert(end, chunk)
            end = self._buf.get_end_iter()
            self._buf.insert_with_tags(
                end, m.group(), *tags, mention_tag,
            )
            pos = m.end()
        if pos < len(text):
            end = self._buf.get_end_iter()
            chunk = text[pos:]
            if tags:
                self._buf.insert_with_tags(end, chunk, *tags)
            else:
                self._buf.insert(end, chunk)

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
            adj = self._msg_sw.get_vadjustment()
            adj.set_value(adj.get_upper() - adj.get_page_size())
            return False
        GLib.idle_add(scroll)

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
                        self._append_message(data)
                        self._last_msg_id = msg_id
                        self._scroll_to_bottom()
                else:
                    ch_row = self._channel_rows.get((net, ch))
                    if ch_row:
                        ch_row.set_unread(True)
            elif nick:
                if (net == self._current_network
                        and nick == self._current_query):
                    msg_id = data.get("id", "")
                    if msg_id != self._last_msg_id:
                        self._append_message(data)
                        self._last_msg_id = msg_id
                        self._scroll_to_bottom()
                else:
                    q_row = self._query_rows.get((net, nick))
                    if q_row:
                        q_row.set_unread(True)
                    elif net:
                        self._add_query_row(net, nick, unread=True)

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
        login = LoginWindow(self)
        login.present()

    def open_main_window(self) -> None:
        win = MainWindow(self)
        win.present()
