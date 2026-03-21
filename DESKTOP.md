# Desktop Integration

These steps follow the freedesktop.org standards and should work on any
compliant Linux desktop (GNOME, KDE, Xfce, LXDE, etc.).

## Desktop Entry

Create `~/.local/share/applications/org.pacujo.lip2.desktop`:

```ini
[Desktop Entry]
Type=Application
Name=Lip2
GenericName=IRC Client
Comment=Lip2 IRC Client
Icon=org.pacujo.lip2
DBusActivatable=false
Exec=python -m lip2
Path=/path/to/lip2
Terminal=false
StartupWMClass=org.pacujo.lip2
Categories=GTK;Network;InstantMessaging;IRC;
```

Set `Path` to the directory containing the `lip2` package.

## Icon

Install the icon into the hicolor theme so that the window manager can
display it in the window decoration and taskbar:

```sh
mkdir -p ~/.local/share/icons/hicolor/256x256/apps
cp icon.png ~/.local/share/icons/hicolor/256x256/apps/org.pacujo.lip2.png
gtk-update-icon-cache ~/.local/share/icons/hicolor/
```

The desktop file name, the `Icon` value, the `StartupWMClass`, and the
icon filename must all use the GTK application ID (`org.pacujo.lip2`)
for the window manager to match them.

## Apply

You may need to restart your panel or log out and back in for the
changes to take effect. For example, on LXDE:

```sh
lxpanelctl restart
```

Lip2 should then appear in the application menu and can be pinned to
the panel or dock.
