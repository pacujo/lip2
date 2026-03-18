from __future__ import annotations

import json
from typing import Any, Generator
from urllib.parse import quote

import httpx


class APIError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(message)


def _ch(channel: str) -> str:
    return quote(channel, safe="")


class LipserviceAPI:
    def __init__(self, base_url: str = "http://127.0.0.1:8080/api") -> None:
        self.base_url = base_url.rstrip("/")
        self.token: str | None = None
        self._username: str | None = None
        self._password: str | None = None
        self._client = httpx.Client(base_url=self.base_url, timeout=10.0)

    @property
    def _auth(self) -> dict[str, str]:
        if self.token:
            return {"Authorization": f"Bearer {self.token}"}
        return {}

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        resp = self._client.request(method, path, headers=self._auth, **kwargs)
        if resp.status_code == 401 and self._username and self._password:
            self._refresh_token()
            resp = self._client.request(
                method, path, headers=self._auth, **kwargs,
            )
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", {})
                if isinstance(detail, dict):
                    msg = detail.get("message", resp.text)
                else:
                    msg = str(detail)
            except Exception:
                msg = resp.text
            raise APIError(resp.status_code, msg)
        if resp.status_code == 204:
            return None
        return resp.json()

    def _refresh_token(self) -> None:
        if not self._username or not self._password:
            return
        resp = self._client.request("POST", "/auth/token", json={
            "username": self._username, "password": self._password,
        })
        if resp.status_code < 400:
            self.token = resp.json().get("token")

    def login(self, username: str, password: str) -> str:
        self._username = username
        self._password = password
        data = self._request("POST", "/auth/token", json={
            "username": username, "password": password,
        })
        self.token = data["token"]
        return self.token

    def list_networks(self) -> list[dict[str, Any]]:
        return self._request("GET", "/networks")

    def get_network(self, name: str) -> dict[str, Any]:
        return self._request("GET", f"/networks/{quote(name, safe='')}")

    def create_network(
        self, name: str, host: str, port: int, tls: bool, nick: str,
        server_password: str | None = None,
        nickserv_password: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "name": name, "host": host, "port": port,
            "tls": tls, "nick": nick,
        }
        if server_password:
            body["server_password"] = server_password
        if nickserv_password:
            body["nickserv_password"] = nickserv_password
        return self._request("POST", "/networks", json=body)

    def connect_network(self, name: str) -> dict[str, Any]:
        return self._request(
            "POST", f"/networks/{quote(name, safe='')}/connect",
        )

    def disconnect_network(self, name: str) -> dict[str, Any]:
        return self._request(
            "POST", f"/networks/{quote(name, safe='')}/disconnect",
        )

    def delete_network(self, name: str) -> None:
        self._request(
            "DELETE", f"/networks/{quote(name, safe='')}",
        )

    def list_channels(self, network: str) -> list[dict[str, Any]]:
        return self._request(
            "GET", f"/networks/{quote(network, safe='')}/channels",
        )

    def list_messages(
        self, network: str, channel: str,
        limit: int = 200, before: str | None = None,
        after: str | None = None, around: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if before:
            params["before"] = before
        if after:
            params["after"] = after
        if around:
            params["around"] = around
        return self._request(
            "GET",
            f"/networks/{quote(network, safe='')}/channels/{_ch(channel)}/messages",
            params=params,
        )

    def search_messages(
        self, network: str, channel: str, query: str,
        anchor: str | None = None, direction: str = "backward",
        limit: int = 1,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "q": query, "direction": direction, "limit": limit,
        }
        if anchor:
            params["anchor"] = anchor
        return self._request(
            "GET",
            f"/networks/{quote(network, safe='')}/channels/{_ch(channel)}/messages/search",
            params=params,
        )

    def join_channel(
        self, network: str, channel: str, key: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"name": channel}
        if key:
            body["key"] = key
        return self._request(
            "POST",
            f"/networks/{quote(network, safe='')}/channels",
            json=body,
        )

    def part_channel(self, network: str, channel: str) -> None:
        self._request(
            "DELETE",
            f"/networks/{quote(network, safe='')}/channels/{_ch(channel)}",
        )

    def send_message(
        self, network: str, channel: str, text: str,
        msg_type: str = "privmsg",
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/networks/{quote(network, safe='')}/channels/{_ch(channel)}/messages",
            json={"text": text, "type": msg_type},
        )

    def list_queries(self, network: str) -> list[dict[str, str]]:
        return self._request(
            "GET",
            f"/networks/{quote(network, safe='')}/queries",
        )

    def list_private_messages(
        self, network: str, nick: str,
        limit: int = 200, before: str | None = None,
        after: str | None = None, around: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if before:
            params["before"] = before
        if after:
            params["after"] = after
        if around:
            params["around"] = around
        return self._request(
            "GET",
            f"/networks/{quote(network, safe='')}/messages/{quote(nick, safe='')}",
            params=params,
        )

    def search_private_messages(
        self, network: str, nick: str, query: str,
        anchor: str | None = None, direction: str = "backward",
        limit: int = 1,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "q": query, "direction": direction, "limit": limit,
        }
        if anchor:
            params["anchor"] = anchor
        return self._request(
            "GET",
            f"/networks/{quote(network, safe='')}/messages/{quote(nick, safe='')}/search",
            params=params,
        )

    def send_private_message(
        self, network: str, nick: str, text: str,
        msg_type: str = "privmsg",
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/networks/{quote(network, safe='')}/messages/{quote(nick, safe='')}",
            json={"text": text, "type": msg_type},
        )

    def close_query(self, network: str, nick: str) -> None:
        self._request(
            "DELETE",
            f"/networks/{quote(network, safe='')}/messages/{quote(nick, safe='')}",
        )

    def get_session(self) -> dict[str, Any]:
        return self._request("GET", "/session")

    def save_session(self, data: dict[str, Any]) -> None:
        self._request("PUT", "/session", json=data)

    def event_stream(self) -> Generator[dict[str, Any], None, None]:
        self._refresh_token()
        stream_client = httpx.Client(
            base_url=self.base_url, timeout=None,
        )
        try:
            with stream_client.stream(
                "GET", "/events",
                headers={**self._auth, "Accept": "text/event-stream"},
            ) as resp:
                event_type = ""
                event_data = ""
                event_id = ""
                for line in resp.iter_lines():
                    if line.startswith("event:"):
                        event_type = line[6:].strip()
                    elif line.startswith("data:"):
                        event_data = line[5:].strip()
                    elif line.startswith("id:"):
                        event_id = line[3:].strip()
                    elif line.startswith(":"):
                        continue
                    elif line == "":
                        if event_type and event_data:
                            try:
                                parsed = json.loads(event_data)
                            except json.JSONDecodeError:
                                parsed = {"raw": event_data}
                            yield {
                                "event": event_type,
                                "id": event_id,
                                "data": parsed,
                            }
                        event_type = ""
                        event_data = ""
                        event_id = ""
        finally:
            stream_client.close()

    def close(self) -> None:
        self._client.close()
