"""Minimal U by Moen cloud client (REST + Pusher) used by the UByMoen plugin.

Moen publishes no official API. Endpoints and message shapes follow the
MIT-licensed community project https://github.com/WeaveHubHQ/ha-u-by-moen
(Jason Lazerus). Credentials and tokens are never logged.

Implementation note: this file deliberately contains no ``class`` statements.
The plugin scaffold treats every class in a node-class file as a device node,
so the helper classes (exceptions, MoenClient, _PusherConn) are assembled with
``type()`` from the plain functions registered below. Behaviour is the same as
ordinary classes.
"""
import json
import logging
import random
import threading
import time

import requests
import websocket

try:
    from udi_interface import LOGGER
except Exception:  # pragma: no cover - tests run without udi_interface logging
    LOGGER = logging.getLogger("ubymoen")

API_BASE = "https://www.moen-iot.com"
HTTP_TIMEOUT = 15
MIN_TEMP_F = 60
DEFAULT_MAX_TEMP_F = 115

MODE_INDEX = {"off": 0, "adjusting": 1, "ready": 2, "pause": 3, "paused-by-preset": 4}
POWER_ON_MODES = ("adjusting", "ready", "pause")

VOLATILE_FIELDS = frozenset(
    {
        "mode",
        "current_temperature",
        "target_temperature",
        "outlets",
        "active_preset",
        "timer_enabled",
        "time_remaining",
    }
)
REST_STATE_KEYS = (
    "mode",
    "current_temperature",
    "target_temperature",
    "max_temp",
    "outlets",
    "active_preset",
    "presets",
    "time_remaining",
    "timer_enabled",
    "timer_length",
    "name",
    "current_firmware_version",
)

# Exceptions (built with type(); see module docstring)
MoenError = type("MoenError", (Exception,), {"__doc__": "Any U by Moen API / transport / command problem."})
MoenAuthError = type("MoenAuthError", (MoenError,), {"__doc__": "Login was rejected."})
MoenHttpError = type("MoenHttpError", (MoenError,), {"__doc__": "HTTP error status from the Moen API.", "status": 0})


def _http_error(method, path, status):
    err = MoenHttpError("U by Moen %s %s returned HTTP %d" % (method.upper(), path, status))
    err.status = status
    return err


# ---------------------------------------------------------------- helpers

def mode_to_index(mode):
    return MODE_INDEX.get(mode)


def is_powered_on(mode):
    return mode in POWER_ON_MODES


def control_payload(action, params=None):
    data = {"action": action}
    if params is not None:
        data["params"] = params
    return {"type": "control", "data": data}


def power_payload(turn_on):
    if turn_on:
        return control_payload("shower_on", {})
    return control_payload("shower_off")


def temperature_payload(temperature, minimum, maximum):
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
        raise MoenError("Temperature must be numeric")
    if not (temperature == temperature) or not minimum <= temperature <= maximum:
        raise MoenError("Temperature must be between %g and %g" % (minimum, maximum))
    target = int(round(temperature))
    return control_payload("temperature_set", {"target_temperature": target}), target


def outlet_payload(outlets, position, active):
    if not outlets or any(
        not isinstance(o, dict)
        or not isinstance(o.get("position"), int)
        or not isinstance(o.get("active"), bool)
        for o in outlets
    ):
        raise MoenError("Current outlet state is incomplete")
    found = False
    updated = []
    for outlet in outlets:
        item = dict(outlet)
        if item["position"] == position:
            item["active"] = active
            found = True
        updated.append(item)
    if not found:
        raise MoenError("Outlet %s is not available" % position)
    return control_payload("outlets_set", {"outlets": updated}), updated


def preset_payload(presets, position):
    preset = next((p for p in presets if isinstance(p, dict) and p.get("position") == position), None)
    if preset is None:
        raise MoenError("Preset %s is not available" % position)
    if position <= 2:
        return control_payload("shower_on", {"preset": position}), dict(preset)
    return control_payload("shower_set", dict(preset)), dict(preset)


def _decode(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def parse_state_event(payload):
    """Normalize a client-state-reported payload -> (event_type, update dict)."""
    envelope = _decode(payload)
    if not isinstance(envelope, dict):
        return None, {}
    event_type = envelope.get("type")
    body = _decode(envelope.get("data", {}))
    if not isinstance(body, dict):
        body = {}
    aliases = {
        "current_mode": "mode",
        "current_temperature": "current_temperature",
        "target_temperature": "target_temperature",
        "outlets": "outlets",
        "active_preset": "active_preset",
        "timer_enabled": "timer_enabled",
        "time_remaining": "time_remaining",
        "presets": "presets",
        "timer_length": "timer_length",
    }
    update = {}
    for source, target in aliases.items():
        if source in body:
            update[target] = body[source]
    if "current_firmware_version" in envelope:
        update["current_firmware_version"] = envelope["current_firmware_version"]
    return (event_type if isinstance(event_type, str) else None), update


def merge_presets(existing, incoming):
    """Merge preset lists by position: incoming entries add to / refresh existing ones.

    Live (Pusher) reports can list fewer presets than the REST lookup, so they
    must never remove presets that are already known.
    """
    by_pos = {}
    for item in existing or []:
        if isinstance(item, dict) and isinstance(item.get("position"), int):
            by_pos[item["position"]] = dict(item)
    for item in incoming or []:
        if isinstance(item, dict) and isinstance(item.get("position"), int):
            combined = dict(by_pos.get(item["position"], {}))
            combined.update(item)
            by_pos[item["position"]] = combined
    return [by_pos[pos] for pos in sorted(by_pos)]


def merge_state(existing, update, authoritative=False):
    """Merge a partial update, keeping per-outlet metadata.

    ``authoritative=True`` (full REST lookups) lets the update replace the
    preset list outright, so presets deleted in the Moen app disappear. Live
    updates leave it False and only add to / refresh known presets.
    """
    merged = dict(existing)
    for key, value in update.items():
        if key == "outlets" and isinstance(value, list):
            by_pos = {
                o.get("position"): dict(o)
                for o in existing.get("outlets", [])
                if isinstance(o, dict) and o.get("position") is not None
            }
            result = []
            for item in value:
                if not isinstance(item, dict):
                    continue
                combined = dict(by_pos.get(item.get("position"), {}))
                combined.update(item)
                result.append(combined)
            merged[key] = result
        elif key == "presets" and isinstance(value, list) and not authoritative:
            merged[key] = merge_presets(existing.get("presets"), value)
        else:
            merged[key] = value
    return merged


def rest_to_state(details):
    return {k: details[k] for k in REST_STATE_KEYS if k in details}


# ------------------------------------------------- method registries

def _registry(store):
    def decorator(func):
        store[func.__name__] = func
        return func
    return decorator


_CLIENT_METHODS = {"__doc__": "REST login/discovery plus live Pusher control for the account's showers."}
_CONN_METHODS = {"__doc__": "One supervised Pusher WebSocket (runs in its own daemon thread)."}
_client_method = _registry(_CLIENT_METHODS)
_conn_method = _registry(_CONN_METHODS)


# ---------------------------------------------------------- MoenClient

@_client_method
def __init__(self, email, password, state_callback):
    self._email = email
    self._password = password
    self._state_cb = state_callback
    self._token = None
    self._lock = threading.RLock()
    self._http = requests.Session()
    self._conns = {}
    self._serial_conn = {}


@_client_method
def authenticate(self):
    with self._lock:
        try:
            resp = self._http.get(
                API_BASE + "/v2/authenticate",
                params={"email": self._email, "password": self._password},
                timeout=HTTP_TIMEOUT,
            )
        except requests.RequestException as err:
            raise MoenError("Authentication request failed (%s)" % type(err).__name__) from None
        if resp.status_code in (401, 403):
            raise MoenAuthError("Invalid U by Moen credentials")
        if resp.status_code >= 400:
            raise MoenError("Authentication returned HTTP %d" % resp.status_code)
        try:
            data = resp.json()
        except ValueError:
            raise MoenError("Authentication response was not JSON") from None
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise MoenAuthError("Authentication response did not contain a token")
        self._token = token
        return token


@_client_method
def _ensure_token(self):
    with self._lock:
        if self._token is None:
            return self.authenticate()
        return self._token


@_client_method
def _request(self, method, path, headers=None, params=None, data=None,
             user_token_header=True, retry_auth=True, allow_empty=False):
    token = self._ensure_token()
    hdrs = dict(headers or {})
    if user_token_header and "User-Token" not in hdrs:
        hdrs["User-Token"] = token
    try:
        resp = self._http.request(
            method, API_BASE + path, headers=hdrs, params=params, data=data,
            timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException as err:
        raise MoenError("U by Moen %s %s failed (%s)" % (method.upper(), path, type(err).__name__)) from None
    if resp.status_code == 401 and retry_auth:
        with self._lock:
            self._token = None
        fresh = self.authenticate()
        new_params = dict(params) if params is not None else None
        new_data = dict(data) if data is not None else None
        if new_params is not None and "user_token" in new_params:
            new_params["user_token"] = fresh
        if new_data is not None and "user_token" in new_data:
            new_data["user_token"] = fresh
        return self._request(method, path, headers=headers, params=new_params, data=new_data,
                             user_token_header=user_token_header, retry_auth=False,
                             allow_empty=allow_empty)
    if resp.status_code in (401, 403):
        raise MoenAuthError("U by Moen %s %s was not authorized" % (method.upper(), path))
    if resp.status_code >= 400:
        raise _http_error(method, path, resp.status_code)
    if resp.status_code == 204 or allow_empty or not resp.content:
        return None
    try:
        return resp.json()
    except ValueError:
        raise MoenError("U by Moen %s %s returned invalid JSON" % (method.upper(), path)) from None


@_client_method
def get_devices(self):
    devices = self._request("GET", "/v2/showers")
    if not isinstance(devices, list):
        raise MoenError("Showers response was not a list")
    return devices


@_client_method
def get_details(self, serial):
    details = self._request("GET", "/v5/showers/%s" % serial)
    if not isinstance(details, dict):
        raise MoenError("Shower detail response was not an object")
    return details


@_client_method
def _get_pusher_credentials(self, serial):
    token = self._ensure_token()
    try:
        creds = self._request(
            "GET", "/v2/credentials", user_token_header=False,
            params={"user_token": token, "serial_number": serial},
        )
        version = 2
    except MoenHttpError as err:
        if err.status not in (404, 405):
            raise MoenError("Failed to get Pusher credentials") from None
        creds = self._request("GET", "/v3/credentials")
        version = 3
    if not isinstance(creds, dict) or not creds.get("app_key") or not creds.get("cluster"):
        raise MoenError("Pusher credentials were incomplete")
    out = dict(creds)
    out["auth_version"] = version
    return out


@_client_method
def get_pusher_auth(self, channel, socket_id, serial, auth_version):
    token = self._ensure_token()
    form = {"channel_name": channel, "socket_id": socket_id}
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if auth_version == 2:
        form["user_token"] = token
        form["serial_number"] = serial
        result = self._request("POST", "/v2/pusher-auth", headers=headers, data=form,
                               user_token_header=False)
    else:
        result = self._request("POST", "/v3/pusher-auth", headers=headers, data=form)
    auth = result.get("auth") if isinstance(result, dict) else None
    if not isinstance(auth, str) or not auth:
        raise MoenError("Pusher authorization response was incomplete")
    return auth


@_client_method
def _ensure_mobile_capability(self, details):
    caps = {c.get("name") for c in details.get("capabilities", []) if isinstance(c, dict)}
    if "pusher_hmi" not in caps or "mobile_supports_pusher" in caps:
        return
    shower_token = details.get("token")
    if not shower_token:
        return
    try:
        self._request(
            "POST", "/v2/capabilities", headers={"Shower-Token": shower_token},
            params={"name": "mobile_supports_pusher"}, user_token_header=False,
            allow_empty=True,
        )
    except MoenError as err:
        LOGGER.debug("Could not register mobile Pusher capability: %s", err)


@_client_method
def register_device(self, details):
    serial = details.get("serial_number")
    if not serial:
        raise MoenError("Device detail omitted serial_number")
    self._ensure_mobile_capability(details)
    creds = self._get_pusher_credentials(serial)
    channel_id = creds.get("channel") or details.get("channel")
    if not channel_id:
        raise MoenError("Pusher credentials omitted channel")
    channel = str(channel_id)
    if not channel.startswith("private-"):
        channel = "private-" + channel
    key = (str(creds["app_key"]), str(creds["cluster"]))
    with self._lock:
        conn = self._conns.get(key)
        if conn is None:
            conn = _PusherConn(self, key[0], key[1])
            self._conns[key] = conn
        conn.add_sub(serial, channel, int(creds.get("auth_version", 3)))
        self._serial_conn[serial] = conn


@_client_method
def start_pusher(self):
    for conn in list(self._conns.values()):
        conn.start()


@_client_method
def stop(self):
    for conn in list(self._conns.values()):
        conn.stop()
    self._conns = {}
    self._serial_conn = {}
    try:
        self._http.close()
    except Exception:
        pass


@_client_method
def is_healthy(self, serial):
    conn = self._serial_conn.get(serial)
    return conn is not None and conn.is_healthy(serial)


@_client_method
def request_report(self, serial):
    conn = self._serial_conn.get(serial)
    if conn is None:
        raise MoenError("No Pusher connection for %s" % serial)
    conn.request_report(serial)


@_client_method
def send_control(self, serial, payload):
    conn = self._serial_conn.get(serial)
    if conn is None:
        raise MoenError("No Pusher connection for %s" % serial)
    conn.send_event(serial, "client-state-desired", payload)


@_client_method
def _on_event(self, serial, event, payload):
    if event != "client-state-reported":
        return
    event_type, update = parse_state_event(payload)
    if event_type == "boot":
        try:
            self.request_report(serial)
        except MoenError as err:
            LOGGER.debug("Could not request report after boot: %s", err)
        return
    if event_type in ("state_change", "shower_report") and update:
        try:
            self._state_cb(serial, update)
        except Exception as err:  # keep the transport thread alive
            LOGGER.error("State callback failed: %s", err)


# --------------------------------------------------------- _PusherConn

@_conn_method
def __init__(self, client, app_key, cluster):
    self.client = client
    self.app_key = app_key
    self.cluster = cluster
    self.subs = {}
    self.ws = None
    self.socket_id = None
    self.thread = None
    self.running = False
    self.send_lock = threading.Lock()
    self.report_id = 0


@_conn_method
def add_sub(self, serial, channel, auth_version):
    self.subs[channel] = {
        "serial": serial,
        "channel": channel,
        "auth_version": auth_version,
        "ready": threading.Event(),
    }


@_conn_method
def is_healthy(self, serial):
    return any(s["serial"] == serial and s["ready"].is_set() for s in self.subs.values())


@_conn_method
def start(self):
    if self.thread is not None and self.thread.is_alive():
        return
    self.running = True
    self.thread = threading.Thread(target=self._supervise, name="moen-pusher", daemon=True)
    self.thread.start()


@_conn_method
def stop(self):
    self.running = False
    ws = self.ws
    if ws is not None:
        try:
            ws.close()
        except Exception:
            pass
    th = self.thread
    if th is not None and th is not threading.current_thread():
        th.join(5)
    self.thread = None
    self._mark_disconnected()


@_conn_method
def _mark_disconnected(self):
    self.socket_id = None
    for sub in self.subs.values():
        sub["ready"].clear()


@_conn_method
def _supervise(self):
    attempt = 0
    while self.running:
        established = False
        try:
            self._run_connection()
        except Exception as err:
            if self.running:
                LOGGER.warning("Pusher connection lost: %s", type(err).__name__)
        finally:
            established = self.socket_id is not None
            self._mark_disconnected()
            ws = self.ws
            self.ws = None
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass
        if not self.running:
            break
        attempt = 1 if established else attempt + 1
        delay = min(30.0, float(attempt * attempt)) + random.uniform(0, 1)
        end = time.time() + delay
        while self.running and time.time() < end:
            time.sleep(0.5)


@_conn_method
def _run_connection(self):
    url = "wss://ws-%s.pusher.com/app/%s?protocol=7&client=python-client&version=1.0" % (
        self.cluster, self.app_key)
    ws = websocket.create_connection(url, timeout=10)
    self.ws = ws
    activity_timeout = 120.0
    last_activity = time.monotonic()
    ping_sent = None
    while self.running:
        if ping_sent is None:
            wait = activity_timeout - (time.monotonic() - last_activity)
        else:
            wait = 30.0 - (time.monotonic() - ping_sent)
        if wait <= 0:
            if ping_sent is not None:
                raise MoenError("Timed out waiting for Pusher pong")
            self._send({"event": "pusher:ping"})
            ping_sent = time.monotonic()
            continue
        ws.settimeout(max(0.5, min(wait, 5.0)))
        try:
            raw = ws.recv()
        except websocket.WebSocketTimeoutException:
            continue
        if not raw:
            raise MoenError("Pusher WebSocket closed")
        last_activity = time.monotonic()
        ping_sent = None
        negotiated = self._handle(raw)
        if negotiated:
            activity_timeout = negotiated


@_conn_method
def _handle(self, raw):
    try:
        msg = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(msg, dict):
        return None
    event = msg.get("event")
    data = msg.get("data")
    if event == "pusher:connection_established":
        info = _decode(data)
        if not isinstance(info, dict) or not info.get("socket_id"):
            raise MoenError("Pusher connection omitted socket_id")
        self.socket_id = str(info["socket_id"])
        for sub in list(self.subs.values()):
            self._subscribe(sub)
        timeout = info.get("activity_timeout")
        if isinstance(timeout, (int, float)) and not isinstance(timeout, bool):
            return max(1.0, float(timeout))
    elif event == "pusher:ping":
        self._send({"event": "pusher:pong"})
    elif event == "pusher:error":
        raise MoenError("Pusher returned an error")
    elif event == "pusher_internal:subscription_succeeded":
        sub = self.subs.get(msg.get("channel"))
        if sub is not None:
            sub["ready"].set()
            LOGGER.info("Subscribed to live U by Moen updates for %s", sub["serial"])
            self.request_report(sub["serial"])
    elif isinstance(event, str):
        sub = self.subs.get(msg.get("channel"))
        if sub is not None:
            self.client._on_event(sub["serial"], event, _decode(data))
    return None


@_conn_method
def _subscribe(self, sub):
    if self.socket_id is None:
        raise MoenError("Pusher socket is not established")
    auth = self.client.get_pusher_auth(
        sub["channel"], self.socket_id, sub["serial"], sub["auth_version"])
    self._send({"event": "pusher:subscribe", "data": {"channel": sub["channel"], "auth": auth}})


@_conn_method
def _send(self, message):
    ws = self.ws
    if ws is None:
        raise MoenError("Pusher WebSocket is disconnected")
    with self.send_lock:
        try:
            ws.send(json.dumps(message, separators=(",", ":")))
        except (websocket.WebSocketException, OSError) as err:
            raise MoenError("Pusher send failed (%s)" % type(err).__name__) from None


@_conn_method
def _find(self, serial):
    for sub in self.subs.values():
        if sub["serial"] == serial:
            return sub
    raise MoenError("No Pusher channel for %s" % serial)


@_conn_method
def request_report(self, serial):
    sub = self._find(serial)
    if not sub["ready"].is_set():
        raise MoenError("Pusher channel is not subscribed")
    self.report_id = (self.report_id + 1) % 1000
    payload = {"jsonrpc": "2.0", "method": "do_shower_report", "id": self.report_id}
    self._send({
        "event": "client-command",
        "channel": sub["channel"],
        "data": json.dumps(payload, separators=(",", ":")),
    })


@_conn_method
def send_event(self, serial, event, payload, timeout=10.0):
    sub = self._find(serial)
    if not sub["ready"].wait(timeout):
        raise MoenError("Pusher channel did not become ready")
    self._send({
        "event": event,
        "channel": sub["channel"],
        "data": json.dumps(payload, separators=(",", ":")),
    })


MoenClient = type("MoenClient", (), dict(_CLIENT_METHODS))
_PusherConn = type("_PusherConn", (), dict(_CONN_METHODS))
