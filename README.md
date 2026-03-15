# Lip2

A GTK 4 client for the [Lipservice](https://github.com/pacujo/lipservice) IRC proxy.

## Prerequisites

- Python 3.11+
- GTK 4 (`gtk4-devel` on Fedora)
- PyGObject (`python3-gobject` on Fedora)

## Quick Start

```bash
pip install -r requirements.txt
python -m lip2
```

The login window appears on startup. Enter the proxy URL
(default `http://127.0.0.1:8080/api`), your username, and password.

## Usage

1. Start the Lipservice proxy in a separate terminal.
2. Launch Lip2 and log in.
3. Click a channel in the sidebar to view messages.
4. Type a message and press Enter to send.
5. Use `/me` for actions (e.g. `/me waves`).

Messages arrive in real time via the proxy's SSE event stream.

## Project Structure

```
lip2/
├── lip2/
│   ├── __main__.py    Entry point
│   ├── api.py         REST + SSE client for Lipservice
│   └── app.py         GTK 4 application, windows, and widgets
└── requirements.txt
```

## Credits

Brunt work by Claude (Anthropic).

## License

Apache License 2.0 -- see [LICENSE](LICENSE).
