"""Shower node and connection orchestration for the UByMoen plugin."""
import threading

import udi_interface

from moen_client import (
    DEFAULT_MAX_TEMP_F,
    MIN_TEMP_F,
    VOLATILE_FIELDS,
    MoenAuthError,
    MoenClient,
    MoenError,
    is_powered_on,
    merge_state,
    mode_to_index,
    outlet_payload,
    power_payload,
    preset_payload,
    rest_to_state,
    temperature_payload,
)

Node = udi_interface.Node
LOGGER = udi_interface.LOGGER

MAX_PRESET_POSITION = 10


class ShowerNode(Node):
    id = "shower"
    drivers = [
        {'driver': 'ST', 'value': 0, 'uom': 25, 'name': 'Shower Power'},
        {'driver': 'CLITEMP', 'value': 0, 'uom': 17, 'name': 'Current Temperature'},
        {'driver': 'CLISPH', 'value': 0, 'uom': 17, 'name': 'Target Temperature'},
        {'driver': 'MODE', 'value': 0, 'uom': 25, 'name': 'Mode'},
        {'driver': 'PRESET', 'value': 0, 'uom': 25, 'name': 'Active Preset'},
        {'driver': 'TIMEREM', 'value': 0, 'uom': 58, 'name': 'Time Remaining'},
        {'driver': 'OUTLET1', 'value': 0, 'uom': 25, 'name': 'Outlet 1'},
        {'driver': 'OUTLET2', 'value': 0, 'uom': 25, 'name': 'Outlet 2'},
        {'driver': 'OUTLET3', 'value': 0, 'uom': 25, 'name': 'Outlet 3'},
        {'driver': 'OUTLET4', 'value': 0, 'uom': 25, 'name': 'Outlet 4'},
    ]

    def __init__(self, polyglot, primary, address, name, serial, moen):
        super().__init__(polyglot, primary, address, name)
        self.moen_serial = serial
        self.moen = moen
        self.shower_state = {}
        self._state_lock = threading.RLock()
        self._preset_sig = None

    # ------------------------------------------------------------ state
    def apply_state(self, update, authoritative=False):
        """Merge an update into the cached state and publish it.

        authoritative=True is for full REST lookups, which may replace the
        preset list. Live (Pusher) and optimistic updates leave it False so
        they can only add to / refresh presets, never drop them.
        """
        if not update:
            return
        with self._state_lock:
            self.shower_state = merge_state(self.shower_state, update, authoritative=authoritative)
            snapshot = dict(self.shower_state)
        try:
            self._publish(snapshot)
        except Exception as err:
            LOGGER.error('Could not publish shower state: %s', err)

    @staticmethod
    def _sorted_outlets(state):
        outlets = state.get('outlets')
        if not isinstance(outlets, list):
            return []
        good = [o for o in outlets if isinstance(o, dict) and isinstance(o.get('position'), int)]
        return sorted(good, key=lambda o: o['position'])

    def _set_temp(self, driver, value):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            self.setDriver(driver, int(round(value)))

    def _set_preset_driver(self, position, title):
        try:
            self.setDriver('PRESET', position, text=title)
        except TypeError:
            self.setDriver('PRESET', position)

    def _publish(self, st):
        mode = st.get('mode')
        index = mode_to_index(mode)
        if index is not None:
            self.setDriver('MODE', index)
            self.setDriver('ST', 1 if is_powered_on(mode) else 0)
        elif mode is not None:
            LOGGER.warning('Unknown shower mode %r', mode)
        self._set_temp('CLITEMP', st.get('current_temperature'))
        self._set_temp('CLISPH', st.get('target_temperature'))

        presets = st.get('presets')
        if isinstance(presets, list):
            self._update_preset_editors(presets)
        active = st.get('active_preset')
        position = active if isinstance(active, int) and not isinstance(active, bool) else 0
        title = 'None'
        if position and isinstance(presets, list):
            match = next((p for p in presets if isinstance(p, dict) and p.get('position') == position), None)
            title = str((match or {}).get('title') or 'Preset %d' % position)
        self._set_preset_driver(position, title)

        remaining = st.get('time_remaining')
        if isinstance(remaining, (int, float)) and not isinstance(remaining, bool):
            self.setDriver('TIMEREM', int(remaining))
        elif 'time_remaining' in st and remaining is None:
            self.setDriver('TIMEREM', 0)

        for i, outlet in enumerate(self._sorted_outlets(st)[:4]):
            self.setDriver('OUTLET%d' % (i + 1), 1 if outlet.get('active') else 0)

    def _update_preset_editors(self, presets):
        names = {}
        for item in presets:
            if not isinstance(item, dict):
                continue
            pos = item.get('position')
            if isinstance(pos, int) and 1 <= pos <= MAX_PRESET_POSITION:
                names[pos] = str(item.get('title') or 'Preset %d' % pos)
        if not names:
            return
        signature = tuple(sorted(names.items()))
        if signature == self._preset_sig:
            return
        self._preset_sig = signature
        positions = sorted(names)
        pick_subset = ','.join(str(p) for p in positions)
        status_names = {'0': 'None'}
        status_names.update({str(p): names[p] for p in positions})
        editors = [
            {'id': 'shower_preset', 'ranges': [
                {'uom': '25', 'subset': '0,' + pick_subset, 'names': status_names}]},
            {'id': 'shower_preset_pick', 'ranges': [
                {'uom': '25', 'subset': pick_subset,
                 'names': {str(p): names[p] for p in positions}}]},
        ]
        threading.Thread(target=self._push_profile, args=(editors,), daemon=True).start()

    def _push_profile(self, editors):
        try:
            self.poly.updateJsonProfile({'editors': editors}, {'waitResponse': True})
        except Exception as err:
            LOGGER.warning('Could not update preset names: %s', err)

    # --------------------------------------------------------- commands
    @staticmethod
    def _command_value(command):
        if isinstance(command, dict):
            value = command.get('value')
            if value is None and isinstance(command.get('query'), dict):
                for candidate in command['query'].values():
                    return candidate
            return value
        return command

    @staticmethod
    def _parse_bool(value):
        text = str(value).strip().lower()
        if text in ('1', '1.0', 'true', 'on', '100'):
            return True
        if text in ('0', '0.0', 'false', 'off'):
            return False
        return None

    def _request_report_later(self):
        try:
            self.moen.request_report(self.moen_serial)
        except MoenError as err:
            LOGGER.debug('Report request failed: %s', err)

    def _send(self, payload, optimistic=None):
        try:
            self.moen.send_control(self.moen_serial, payload)
        except MoenError as err:
            LOGGER.error('Shower command failed: %s', err)
            return False
        if optimistic:
            self.apply_state(optimistic)
        timer = threading.Timer(3.0, self._request_report_later)
        timer.daemon = True
        timer.start()
        return True

    def shower_on(self, command=None, *args, **kwargs):
        self._send(power_payload(True), {'mode': 'adjusting'})

    def shower_off(self, command=None, *args, **kwargs):
        self._send(power_payload(False), {'mode': 'off'})

    def set_temp(self, command=None, *args, **kwargs):
        try:
            temperature = float(self._command_value(command))
        except (TypeError, ValueError):
            LOGGER.error('Set Temperature needs a numeric value')
            return
        with self._state_lock:
            max_temp = self.shower_state.get('max_temp')
        try:
            maximum = float(max_temp) if max_temp else float(DEFAULT_MAX_TEMP_F)
        except (TypeError, ValueError):
            maximum = float(DEFAULT_MAX_TEMP_F)
        try:
            payload, target = temperature_payload(temperature, MIN_TEMP_F, maximum)
        except MoenError as err:
            LOGGER.error('Set Temperature rejected: %s', err)
            return
        self._send(payload, {'target_temperature': target})

    def start_preset(self, command=None, *args, **kwargs):
        try:
            position = int(float(self._command_value(command)))
        except (TypeError, ValueError):
            LOGGER.error('Start Preset needs a preset number')
            return
        with self._state_lock:
            presets = list(self.shower_state.get('presets') or [])
        try:
            payload, preset = preset_payload(presets, position)
        except MoenError as err:
            LOGGER.error('Start Preset rejected: %s', err)
            return
        optimistic = {'active_preset': position, 'mode': 'adjusting'}
        if isinstance(preset.get('target_temperature'), (int, float)):
            optimistic['target_temperature'] = preset['target_temperature']
        self._send(payload, optimistic)

    def _set_outlet(self, command, index):
        active = self._parse_bool(self._command_value(command))
        if active is None:
            LOGGER.error('Outlet %d needs an On/Off value', index + 1)
            return
        with self._state_lock:
            snapshot = dict(self.shower_state)
        sorted_outlets = self._sorted_outlets(snapshot)
        if index >= len(sorted_outlets):
            LOGGER.error('Outlet %d is not available on this shower', index + 1)
            return
        position = sorted_outlets[index]['position']
        try:
            payload, updated = outlet_payload(snapshot.get('outlets') or [], position, active)
        except MoenError as err:
            LOGGER.error('Outlet %d rejected: %s', index + 1, err)
            return
        self._send(payload, {'outlets': updated})

    def outlet1(self, command=None, *args, **kwargs):
        self._set_outlet(command, 0)

    def outlet2(self, command=None, *args, **kwargs):
        self._set_outlet(command, 1)

    def outlet3(self, command=None, *args, **kwargs):
        self._set_outlet(command, 2)

    def outlet4(self, command=None, *args, **kwargs):
        self._set_outlet(command, 3)

    def query_cmd(self, command=None, *args, **kwargs):
        self._request_report_later()
        self.reportDrivers()

    commands = {
        'DON': shower_on,
        'DOF': shower_off,
        'SET_TEMP': set_temp,
        'START_PRESET': start_preset,
        'OUTLET1': outlet1,
        'OUTLET2': outlet2,
        'OUTLET3': outlet3,
        'OUTLET4': outlet4,
        'QUERY': query_cmd,
    }


# ------------------------------------------------- connection management

_CONNECT_LOCK = threading.RLock()


def _notice(ctrl, key, text=None):
    try:
        if text:
            ctrl.poly.Notices[key] = text
        else:
            ctrl.poly.Notices.delete(key)
    except Exception:
        pass


def shutdown(ctrl):
    client = getattr(ctrl, 'moen', None)
    ctrl.moen = None
    ctrl.moen_nodes = {}
    if client is not None:
        try:
            client.stop()
        except Exception as err:
            LOGGER.debug('Error stopping Moen client: %s', err)


def connect(ctrl):
    """(Re)connect to the Moen cloud and (re)create shower nodes. Returns node count."""
    with _CONNECT_LOCK:
        ctrl.moen_auth_failed = False
        params = getattr(ctrl, 'moen_params', None) or {}
        email = str(params.get('email') or '').strip()
        password = str(params.get('password') or '')
        if not email or not password:
            _notice(ctrl, 'moen_cfg', 'Enter your U by Moen email and password in the plugin configuration.')
            return 0
        _notice(ctrl, 'moen_cfg')
        shutdown(ctrl)

        nodes = {}

        def on_state(serial, update):
            node = nodes.get(serial)
            if node is not None:
                node.apply_state(update)

        client = MoenClient(email, password, on_state)
        try:
            client.authenticate()
            devices = client.get_devices()
        except MoenAuthError:
            ctrl.moen_auth_failed = True
            _notice(ctrl, 'moen_auth', 'U by Moen login failed. Check the email/password (an Alexa/Android account is required).')
            client.stop()
            return 0
        except MoenError as err:
            LOGGER.error('Could not reach U by Moen: %s', err)
            _notice(ctrl, 'moen_net', 'Could not reach the U by Moen cloud. Will retry automatically.')
            client.stop()
            return 0
        _notice(ctrl, 'moen_auth')
        _notice(ctrl, 'moen_net')

        for device in devices:
            serial = device.get('serial_number') if isinstance(device, dict) else None
            if not serial:
                continue
            try:
                details = client.get_details(serial)
            except MoenError as err:
                LOGGER.warning('Could not read details for %s: %s', serial, err)
                details = device
            address = ctrl.poly.getValidAddress('shower_' + str(serial)[-7:])
            name = ctrl.poly.getValidName(str(details.get('name') or 'Shower'))
            node = ShowerNode(ctrl.poly, ctrl.address, address, name, serial, client)
            ctrl.poly.addNode(node)
            nodes[serial] = node
            node.apply_state(rest_to_state(details), authoritative=True)
            try:
                client.register_device(details)
            except MoenError as err:
                LOGGER.warning('Live updates unavailable for %s, using polling: %s', serial, err)

        ctrl.moen = client
        ctrl.moen_nodes = nodes
        client.start_pusher()
        if not nodes:
            _notice(ctrl, 'moen_none', 'No showers were found on this U by Moen account.')
        else:
            _notice(ctrl, 'moen_none')
        return len(nodes)


def refresh_all(ctrl, full):
    """REST refresh. full=False only refreshes showers whose live channel is down."""
    client = getattr(ctrl, 'moen', None)
    nodes = getattr(ctrl, 'moen_nodes', None) or {}
    if client is None:
        return
    for serial, node in list(nodes.items()):
        healthy = client.is_healthy(serial)
        if healthy and not full:
            continue
        try:
            details = client.get_details(serial)
        except MoenAuthError:
            ctrl.moen_auth_failed = True
            _notice(ctrl, 'moen_auth', 'U by Moen login failed. Check the email/password (an Alexa/Android account is required).')
            return
        except MoenError as err:
            LOGGER.warning('Refresh failed for %s: %s', serial, err)
            continue
        update = rest_to_state(details)
        if healthy:
            update = {k: v for k, v in update.items() if k not in VOLATILE_FIELDS}
        node.apply_state(update, authoritative=True)
        if healthy:
            try:
                client.request_report(serial)
            except MoenError:
                pass
