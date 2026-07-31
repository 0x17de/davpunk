"""Desktop notifications over ``org.freedesktop.Notifications``.

``jeepney`` is used rather than ``dbus-python``: it is pure Python, so DavPunk
does not need a C toolchain and ``pkg-config`` for ``dbus-1`` to install.  When
it is unavailable — or there is no session bus, as in a headless test run — we
fall back to ``notify-send`` and then to a log line.  A missing notification
must never take down a sync.
"""

from __future__ import annotations

import logging
import shutil
import subprocess

log = logging.getLogger("davpunk.notifications.dbus_notify")

BUS_NAME = "org.freedesktop.Notifications"
OBJECT_PATH = "/org/freedesktop/Notifications"
INTERFACE = "org.freedesktop.Notifications"

APP_NAME = "DavPunk"
DEFAULT_TIMEOUT_MS = 10_000


class NotificationError(Exception):
    pass


def _notify_jeepney(summary: str, body: str, timeout_ms: int) -> bool:
    try:
        from jeepney import DBusAddress, new_method_call
        from jeepney.io.blocking import open_dbus_connection
    except ImportError:
        return False

    address = DBusAddress(OBJECT_PATH, bus_name=BUS_NAME, interface=INTERFACE)
    message = new_method_call(
        address,
        "Notify",
        "susssasa{sv}i",
        (APP_NAME, 0, "", summary, body, [], {}, timeout_ms),
    )
    try:
        connection = open_dbus_connection(bus="SESSION")
    except Exception as exc:
        log.debug("No session bus for notifications: %s", exc)
        return False
    try:
        connection.send_and_get_reply(message)
        return True
    except Exception as exc:
        log.debug("D-Bus Notify failed: %s", exc)
        return False
    finally:
        connection.close()


def _notify_send(summary: str, body: str, timeout_ms: int) -> bool:
    if shutil.which("notify-send") is None:
        return False
    try:
        subprocess.run(
            ["notify-send", "--app-name", APP_NAME, "-t", str(timeout_ms), summary, body],
            check=False,
            timeout=10,
        )
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("notify-send failed: %s", exc)
        return False


def notify(summary: str, body: str = "", *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> bool:
    """Best-effort delivery.  Returns whether anything actually took it."""
    for backend in (_notify_jeepney, _notify_send):
        if backend(summary, body, timeout_ms):
            return True
    log.info("Notification not delivered (no D-Bus, no notify-send): %s", summary)
    return False


def service_available() -> bool:
    """Whether the notification service is reachable — a ``doctor`` check."""
    try:
        from jeepney import DBusAddress, new_method_call
        from jeepney.io.blocking import open_dbus_connection
    except ImportError:
        return shutil.which("notify-send") is not None

    address = DBusAddress(OBJECT_PATH, bus_name=BUS_NAME, interface=INTERFACE)
    try:
        connection = open_dbus_connection(bus="SESSION")
    except Exception:
        return False
    try:
        connection.send_and_get_reply(new_method_call(address, "GetServerInformation", "", ()))
        return True
    except Exception:
        return False
    finally:
        connection.close()
