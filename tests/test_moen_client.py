import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import moen_client as mc  # noqa: E402


def test_power_payloads():
    assert mc.power_payload(True) == {'type': 'control', 'data': {'action': 'shower_on', 'params': {}}}
    assert mc.power_payload(False) == {'type': 'control', 'data': {'action': 'shower_off'}}


def test_temperature_payload_rounds_and_validates():
    payload, target = mc.temperature_payload(101.6, 60, 115)
    assert target == 102
    assert payload == {'type': 'control', 'data': {'action': 'temperature_set', 'params': {'target_temperature': 102}}}
    with pytest.raises(mc.MoenError):
        mc.temperature_payload(59, 60, 115)
    with pytest.raises(mc.MoenError):
        mc.temperature_payload(116, 60, 115)
    with pytest.raises(mc.MoenError):
        mc.temperature_payload(True, 60, 115)
    with pytest.raises(mc.MoenError):
        mc.temperature_payload('hot', 60, 115)


def test_outlet_payload_changes_only_target_and_keeps_metadata():
    outlets = [{'position': 1, 'active': False, 'type': 'head'}, {'position': 2, 'active': True, 'type': 'hand'}]
    payload, updated = mc.outlet_payload(outlets, 1, True)
    assert payload['data']['action'] == 'outlets_set'
    assert updated[0] == {'position': 1, 'active': True, 'type': 'head'}
    assert updated[1] == outlets[1]
    assert outlets[0]['active'] is False  # input not mutated
    assert payload['data']['params'] == {'outlets': updated}


def test_outlet_payload_errors():
    with pytest.raises(mc.MoenError):
        mc.outlet_payload([], 1, True)
    with pytest.raises(mc.MoenError):
        mc.outlet_payload([{'position': 1, 'active': False}], 5, True)
    with pytest.raises(mc.MoenError):
        mc.outlet_payload([{'position': 1, 'active': 'yes'}], 1, True)


def test_preset_payload_compact_and_full():
    presets = [
        {'position': 1, 'title': 'A', 'target_temperature': 100},
        {'position': 2, 'title': 'B', 'target_temperature': 101},
        {'position': 3, 'title': 'C', 'target_temperature': 102},
    ]
    payload, preset = mc.preset_payload(presets, 2)
    assert payload == {'type': 'control', 'data': {'action': 'shower_on', 'params': {'preset': 2}}}
    assert preset['title'] == 'B'
    payload, preset = mc.preset_payload(presets, 3)
    assert payload['data']['action'] == 'shower_set'
    assert payload['data']['params'] == presets[2]
    with pytest.raises(mc.MoenError):
        mc.preset_payload(presets, 9)


def test_parse_state_event_json_string_and_aliases():
    raw = '{"type": "state_change", "data": {"current_mode": "ready", "target_temperature": 100, "ignored": 1}, "current_firmware_version": "1.2"}'
    event_type, update = mc.parse_state_event(raw)
    assert event_type == 'state_change'
    assert update == {'mode': 'ready', 'target_temperature': 100, 'current_firmware_version': '1.2'}
    assert mc.parse_state_event('not json') == (None, {})
    assert mc.parse_state_event(None) == (None, {})


def test_merge_state_keeps_outlet_metadata():
    existing = {'mode': 'off', 'outlets': [{'position': 1, 'active': False, 'type': 'head'}]}
    merged = mc.merge_state(existing, {'outlets': [{'position': 1, 'active': True}], 'mode': 'ready'})
    assert merged['mode'] == 'ready'
    assert merged['outlets'] == [{'position': 1, 'active': True, 'type': 'head'}]
    assert existing['mode'] == 'off'


FULL_PRESETS = [
    {'position': 1, 'title': 'Preset 1', 'target_temperature': 100},
    {'position': 2, 'title': 'Preset 2', 'target_temperature': 101},
    {'position': 3, 'title': 'Johns Shower', 'target_temperature': 104, 'extra': 'x'},
]
LIVE_PRESETS = [
    {'position': 1, 'title': 'Preset 1', 'target_temperature': 100},
    {'position': 2, 'title': 'Preset 2', 'target_temperature': 101},
]


def test_live_update_with_fewer_presets_does_not_drop_known_presets():
    # Regression: a live report listing only presets 1-2 used to wipe preset 3.
    state = mc.merge_state({}, {'presets': FULL_PRESETS}, authoritative=True)
    state = mc.merge_state(state, {'presets': LIVE_PRESETS})
    assert [p['position'] for p in state['presets']] == [1, 2, 3]
    assert state['presets'][2]['title'] == 'Johns Shower'
    assert state['presets'][2]['extra'] == 'x'
    # preset 3 is still startable
    payload, preset = mc.preset_payload(state['presets'], 3)
    assert payload['data']['action'] == 'shower_set'
    assert preset['title'] == 'Johns Shower'


def test_live_update_refreshes_and_adds_presets():
    state = mc.merge_state({'presets': FULL_PRESETS}, {
        'presets': [{'position': 2, 'title': 'Renamed'}, {'position': 4, 'title': 'New'}]})
    by_pos = {p['position']: p for p in state['presets']}
    assert sorted(by_pos) == [1, 2, 3, 4]
    assert by_pos[2]['title'] == 'Renamed'
    assert by_pos[2]['target_temperature'] == 101  # untouched fields kept
    assert by_pos[4]['title'] == 'New'


def test_authoritative_update_replaces_presets():
    # REST lookups may remove presets deleted in the Moen app.
    state = mc.merge_state({'presets': FULL_PRESETS}, {'presets': LIVE_PRESETS}, authoritative=True)
    assert [p['position'] for p in state['presets']] == [1, 2]


def test_merge_state_does_not_mutate_input_presets():
    existing = {'presets': [dict(p) for p in FULL_PRESETS]}
    mc.merge_state(existing, {'presets': LIVE_PRESETS})
    assert [p['position'] for p in existing['presets']] == [1, 2, 3]


def test_merge_presets_ignores_malformed_entries():
    merged = mc.merge_presets(None, [{'position': 1, 'title': 'A'}, 'junk', {'title': 'no position'}])
    assert merged == [{'position': 1, 'title': 'A'}]
    assert mc.merge_presets(FULL_PRESETS, None) == FULL_PRESETS


def test_mode_helpers():
    assert mc.mode_to_index('off') == 0
    assert mc.mode_to_index('paused-by-preset') == 4
    assert mc.mode_to_index('bogus') is None
    assert mc.is_powered_on('adjusting') and mc.is_powered_on('ready') and mc.is_powered_on('pause')
    assert not mc.is_powered_on('off') and not mc.is_powered_on('paused-by-preset')


def test_rest_to_state_filters_keys():
    state = mc.rest_to_state({'mode': 'off', 'token': 'secret', 'max_temp': 115})
    assert state == {'mode': 'off', 'max_temp': 115}


def test_client_classes_and_http_error_status():
    assert issubclass(mc.MoenAuthError, mc.MoenError)
    assert issubclass(mc.MoenHttpError, mc.MoenError)
    err = mc._http_error('get', '/x', 404)
    assert isinstance(err, mc.MoenHttpError) and err.status == 404
    client = mc.MoenClient('a@b.c', 'pw', lambda serial, update: None)
    assert client.is_healthy('nope') is False
    with pytest.raises(mc.MoenError):
        client.send_control('nope', mc.power_payload(True))
    client.stop()
