from __future__ import annotations

import sys
import threading
import time
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib, Pango  # noqa: E402

from lip2.api import LipserviceAPI, APIError  # noqa: E402


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
        self, network: str, channel: str | None = None, state: str = "",
    ) -> None:
        super().__init__()
        self.network = network
        self.channel = channel
        self.net_state = state
        self._label = Gtk.Label()
        self._label.set_xalign(0)
        self._label.set_margin_start(20 if channel else 8)
        self._label.set_margin_end(8)
        self._label.set_margin_top(3)
        self._label.set_margin_bottom(3)
        self.update(state)
        self.set_child(self._label)
        if not channel:
            self.set_selectable(False)

    def set_unread(self, unread: bool) -> None:
        if not self.channel:
            return
        name = GLib.markup_escape_text(self.channel)
        if unread:
            self._label.set_markup(f"<b>{name}</b>")
        else:
            self._label.set_markup(name)

    def update(self, state: str = "") -> None:
        self.net_state = state
        if self.channel:
            self._label.set_text(self.channel)
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


# -- Main window --------------------------------------------------------------

class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app: Lip2App) -> None:
        super().__init__(application=app, title="Lip2")
        self._app = app
        self.set_default_size(900, 600)

        self._current_network: str | None = None
        self._current_channel: str | None = None
        self._last_msg_id: str | None = None
        self._last_date: str | None = None
        self._network_rows: dict[str, SidebarRow] = {}
        self._channel_rows: dict[tuple[str, str], SidebarRow] = {}
        self._sse_running = False

        self._build_ui()
        self._load_sidebar()
        self._start_sse()

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

        self._msg_sw.set_child(self._msg_view)
        right.append(self._msg_sw)

        input_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=4,
        )
        input_box.set_margin_start(4)
        input_box.set_margin_end(4)
        input_box.set_margin_top(4)
        input_box.set_margin_bottom(4)

        self._input = Gtk.Entry()
        self._input.set_hexpand(True)
        self._input.set_placeholder_text("Type a message...")
        self._input.connect("activate", self._on_send)
        self._input.set_sensitive(False)
        input_box.append(self._input)

        right.append(input_box)
        paned.set_end_child(right)
        self.set_child(paned)

    # -- sidebar loading ------------------------------------------------------

    def _load_sidebar(self) -> None:
        saved_net = self._current_network
        saved_ch = self._current_channel

        def fetch() -> list[dict[str, Any]]:
            api = self._app.api
            networks = api.list_networks()
            for net in networks:
                net["_channels"] = api.list_channels(net["name"])
            return networks

        def populate(networks: list[dict[str, Any]]) -> None:
            self._clear_sidebar()
            self._network_rows.clear()
            self._channel_rows.clear()
            reselect: SidebarRow | None = None
            first_channel: SidebarRow | None = None
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
                    if first_channel is None:
                        first_channel = row
                    if (net["name"] == saved_net
                            and ch["name"] == saved_ch):
                        reselect = row
            if networks and not first_channel:
                self._sidebar.append(self._hint_row(
                    "Right-click to join a channel",
                ))
            pick = reselect or first_channel
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

    # -- channel selection & messages -----------------------------------------

    def _on_row_selected(
        self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow | None,
    ) -> None:
        if not row or not isinstance(row, SidebarRow) or not row.channel:
            return
        self._current_network = row.network
        self._current_channel = row.channel
        row.set_unread(False)
        self._header.set_text(f"{row.network} / {row.channel}")
        self._input.set_sensitive(True)
        self._input.grab_focus()
        self._load_messages()

    def _load_messages(self) -> None:
        net = self._current_network
        ch = self._current_channel
        if not net or not ch:
            return

        def fetch() -> dict[str, Any]:
            return self._app.api.list_messages(net, ch)

        def display(data: dict[str, Any]) -> None:
            if self._current_network != net or self._current_channel != ch:
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
                end, f"* {sender} {msg['text']}", "action",
            )
        elif msg_type == "notice":
            self._buf.insert_with_tags_by_name(
                end, f"-{sender}- ", "nick",
            )
            end = self._buf.get_end_iter()
            self._buf.insert(end, msg["text"])
        else:
            self._buf.insert_with_tags_by_name(
                end, f"<{sender}> ", "nick",
            )
            end = self._buf.get_end_iter()
            self._buf.insert(end, msg["text"])

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
        text = self._input.get_text().strip()
        if not text or not self._current_network or not self._current_channel:
            return
        self._input.set_text("")
        net = self._current_network
        ch = self._current_channel

        msg_type = "privmsg"
        if text.startswith("/me "):
            msg_type = "action"
            text = text[4:]

        def send() -> None:
            self._app.api.send_message(net, ch, text, msg_type)

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
            if net == self._current_network and ch == self._current_channel:
                msg_id = data.get("id", "")
                if msg_id != self._last_msg_id:
                    self._append_message(data)
                    self._last_msg_id = msg_id
                    self._scroll_to_bottom()
            else:
                ch_row = self._channel_rows.get((net, ch))
                if ch_row:
                    ch_row.set_unread(True)

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

    def _make_popover(
        self, x: float, y: float,
    ) -> tuple[Gtk.Popover, Gtk.Box]:
        from gi.repository import Gdk  # noqa: E402
        menu = Gtk.Popover()
        menu.set_parent(self._sidebar)
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
        self._buf.set_text("")
        self._msg_view.grab_focus()
        self._input.set_sensitive(False)

    def _clear_view_if_current(self, network: str, channel: str) -> None:
        if self._current_network == network and self._current_channel == channel:
            self._current_channel = None
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
