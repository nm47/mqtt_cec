"""Controller and MQTT regressions using a simulated TV; no hardware required."""
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from src import cec
from src.cec_controller import CECController
from src.config import Config
from src.mqtt_handler import MQTTHandler

class TV:
    def __init__(self, power=0):
        self.power = power
        self.sent = []
        self.queries = 0
        self.on_query = None
        self.ack = True
    def get_phys_addr(self):
        return 0x1000
    def transmit(self, destination, payload, reply=0, timeout_ms=0):
        if payload[0] == cec.GIVE_DEVICE_POWER_STATUS:
            self.queries += 1
            if self.on_query:
                self.on_query()
            return cec.CECMessage(0, 4, cec.REPORT_POWER_STATUS, bytes([self.power]), tx_status=1, rx_status=1)
        self.sent.append((destination, payload))
        return cec.CECMessage(4, destination, payload[0], payload[1:], tx_status=int(self.ack))

def controller(power=0):
    ctrl = CECController()
    ctrl._tx = TV(power)
    return ctrl

def tv_message(opcode, operands=b''):
    return cec.CECMessage(0, 15, opcode, operands, rx_status=1)

class Regressions(unittest.TestCase):
    def test_power_replies_and_aborts_are_not_wake_hints(self):
        for opcode, operands in ((cec.REPORT_POWER_STATUS, b'\x01'),
                                 (cec.FEATURE_ABORT, b'\x8f\x00')):
            with self.subTest(opcode=opcode):
                ctrl = controller(1)
                ctrl.power = 'OFF'
                ctrl._handle_message(cec.CECMessage(0, 4, opcode, operands, rx_status=1))
                self.assertFalse(ctrl._poll_now.is_set())

    def test_routing_activity_still_checks_for_wake(self):
        ctrl = controller(1)
        ctrl.power = 'OFF'
        ctrl._handle_message(tv_message(cec.REQUEST_ACTIVE_SOURCE))
        self.assertTrue(ctrl._poll_now.is_set())

    def test_duplicate_off_reply_does_not_trigger_another_poll(self):
        ctrl = controller(1)
        ctrl.power = 'OFF'
        def duplicate():
            ctrl._handle_message(cec.CECMessage(0, 4, cec.REPORT_POWER_STATUS, b'\x01', rx_status=1))
            if ctrl._tx.queries == 3:
                ctrl._stop.set()  # bound the feedback loop in this test
        ctrl._tx.on_query = duplicate
        ctrl._poll_now.set()
        thread = threading.Thread(target=ctrl._poll_loop)
        thread.start()
        try:
            thread.join(timeout=0.1)
        finally:
            ctrl._stop.set()
            ctrl._poll_now.set()
            thread.join(timeout=1)
        self.assertEqual(ctrl._tx.queries, 1, 'Duplicate OFF replies must not bypass the 30-second poll interval')

    def test_on_reverses_a_pending_off_even_if_tv_still_reports_on(self):
        ctrl = controller(0)
        ctrl.power = 'ON'
        self.assertTrue(ctrl.power_off())
        self.assertTrue(ctrl.power_on())
        self.assertEqual([p[0] for _, p in ctrl._tx.sent], [cec.STANDBY, cec.IMAGE_VIEW_ON],
                         'ON must supersede the pending OFF command')
        self.assertEqual(ctrl._expected_power, 'ON')

    def test_off_reverses_pending_on_even_if_tv_still_reports_off(self):
        ctrl = controller(1)
        ctrl.power_on()
        ctrl.power_off()
        self.assertEqual([p[0] for _, p in ctrl._tx.sent], [cec.IMAGE_VIEW_ON, cec.STANDBY])
        self.assertEqual(ctrl._expected_power, 'OFF')

    def test_expired_opposite_command_does_not_force_retransmission(self):
        ctrl = controller(0)
        with patch('src.cec_controller.time.monotonic', return_value=100):
            ctrl.power_off()
        with patch('src.cec_controller.time.monotonic', return_value=116):
            ctrl.power_on()
        self.assertEqual([p[0] for _, p in ctrl._tx.sent], [cec.STANDBY])
        self.assertIsNone(ctrl._expected_power)

    def test_failed_reversal_keeps_previous_pending_target(self):
        ctrl = controller(0)
        ctrl.power_off()
        ctrl._tx.ack = False
        self.assertFalse(ctrl.power_on())
        self.assertEqual(ctrl.power, 'OFF')
        self.assertEqual(ctrl._expected_power, 'OFF')

    def test_set_stream_path_away_stops_claiming_pi_as_active(self):
        ctrl = controller()
        ctrl.power = 'ON'
        ctrl.input = 'HDMI1'
        ctrl._active_source = True
        ctrl._handle_message(tv_message(cec.SET_STREAM_PATH, b'\x20\x00'))
        ctrl._handle_message(tv_message(cec.REQUEST_ACTIVE_SOURCE))
        self.assertEqual(ctrl.input, 'HDMI2')
        self.assertEqual(ctrl._tx.sent, [], 'Pi must not reclaim HDMI1 after TV selects HDMI2')

    def test_stream_path_to_pi_still_announces_active_source(self):
        ctrl = controller()
        ctrl._handle_message(tv_message(cec.SET_STREAM_PATH, b'\x10\x00'))
        self.assertTrue(ctrl._active_source)
        self.assertEqual(ctrl._tx.sent, [(15, b'\x82\x10\x00')])

    def test_routing_information_tracks_active_source_in_both_directions(self):
        ctrl = controller()
        ctrl._handle_message(tv_message(cec.ROUTING_INFORMATION, b'\x10\x00'))
        self.assertTrue(ctrl._active_source)
        ctrl._handle_message(tv_message(cec.ROUTING_INFORMATION, b'\x20\x00'))
        self.assertFalse(ctrl._active_source)

    def test_other_active_source_clears_pi_claim(self):
        ctrl = controller()
        ctrl._active_source = True
        ctrl._handle_message(cec.CECMessage(8, 15, cec.ACTIVE_SOURCE, b'\x20\x00'))
        self.assertFalse(ctrl._active_source)

    def test_toggle_uses_current_tv_state_after_remote_change(self):
        ctrl = controller(1)  # TV is actually off, before next periodic poll
        ctrl.power = 'ON'     # cached state from the previous poll
        handler = MQTTHandler(Config(mqtt_broker_host='unused.invalid'), ctrl)
        handler._handle_power_command('TOGGLE')
        self.assertEqual([p[0] for _, p in ctrl._tx.sent], [cec.IMAGE_VIEW_ON],
                         'Toggle should turn an actually OFF TV on, not become an OFF no-op')

    def test_numeric_toggle_queries_on_tv_even_when_cached_unknown(self):
        ctrl = controller(0)
        handler = MQTTHandler(Config(mqtt_broker_host='unused.invalid'), ctrl)
        handler._handle_power_command('2')
        self.assertEqual(ctrl._tx.queries, 1)
        self.assertEqual(ctrl._tx.sent, [(0, bytes([cec.STANDBY]))])

    def test_two_toggles_reverse_pending_command_despite_lagging_tv(self):
        ctrl = controller(0)
        ctrl.toggle_power()
        ctrl.toggle_power()
        self.assertEqual([p[0] for _, p in ctrl._tx.sent], [cec.STANDBY, cec.IMAGE_VIEW_ON])
        self.assertEqual(ctrl._expected_power, 'ON')

    def test_toggle_does_not_guess_from_cache_when_query_fails(self):
        ctrl = controller()
        ctrl.power = 'ON'
        with patch.object(ctrl, 'query_power', return_value=None):
            self.assertFalse(ctrl.toggle_power())
        self.assertEqual(ctrl._tx.sent, [])

    def test_toggle_can_reverse_pending_command_when_query_fails(self):
        ctrl = controller(0)
        ctrl.power_off()
        with patch.object(ctrl, 'query_power', return_value=None):
            self.assertTrue(ctrl.toggle_power())
        self.assertEqual([p[0] for _, p in ctrl._tx.sent], [cec.STANDBY, cec.IMAGE_VIEW_ON])

    def test_toggle_uses_fresh_state_after_pending_command_expires(self):
        ctrl = controller(0)
        with patch('src.cec_controller.time.monotonic', return_value=100):
            ctrl.power_off()
        with patch('src.cec_controller.time.monotonic', return_value=116):
            ctrl.toggle_power()
        self.assertEqual([p[0] for _, p in ctrl._tx.sent], [cec.STANDBY, cec.STANDBY])

class WorkingBehavior(unittest.TestCase):
    def test_queries_actual_tv_state(self):
        ctrl = controller(1)
        ctrl.power = 'ON'
        self.assertEqual(ctrl.query_power(), 'OFF')
    def test_on_is_idempotent_when_no_opposite_command_pending(self):
        ctrl = controller(0)
        self.assertTrue(ctrl.power_on())
        self.assertEqual(ctrl._tx.sent, [])
        self.assertEqual(ctrl.power, 'ON')
    def test_remote_standby_updates_state_and_publishes(self):
        ctrl = controller()
        ctrl.power = 'ON'
        ctrl._active_source = True
        ctrl.on_state_change = Mock()
        ctrl._handle_message(tv_message(cec.STANDBY))
        self.assertEqual(ctrl.power, 'OFF')
        self.assertFalse(ctrl._active_source)
        ctrl.on_state_change.assert_called_once_with('OFF', 'UNKNOWN')
    def test_remote_routing_change_updates_input(self):
        ctrl = controller()
        ctrl._active_source = True
        ctrl._handle_message(tv_message(cec.ROUTING_CHANGE, b'\x10\x00\x30\x00'))
        self.assertEqual(ctrl.input, 'HDMI3')
        self.assertFalse(ctrl._active_source)
    def test_pending_off_ignores_late_on_then_accepts_after_timeout(self):
        ctrl = controller(0)
        with patch('src.cec_controller.time.monotonic', return_value=100):
            ctrl.power_off()
            ctrl._apply_reported_power('ON')
            self.assertEqual(ctrl.power, 'OFF')
        with patch('src.cec_controller.time.monotonic', return_value=116):
            ctrl._apply_reported_power('ON')
            self.assertEqual(ctrl.power, 'ON')
    def test_failed_power_tx_does_not_claim_state_change(self):
        ctrl = controller(1)
        ctrl.power = 'OFF'
        ctrl._tx.ack = False
        self.assertFalse(ctrl.power_on())
        self.assertEqual(ctrl.power, 'OFF')
    def test_failed_input_tx_does_not_claim_state_change(self):
        ctrl = controller()
        ctrl.input = 'HDMI1'
        ctrl._tx.ack = False
        self.assertFalse(ctrl.switch_input('HDMI2'))
        self.assertEqual(ctrl.input, 'HDMI1')
    def test_retained_commands_are_not_replayed(self):
        ctrl = controller(1)
        cfg = Config(mqtt_broker_host='unused.invalid')
        handler = MQTTHandler(cfg, ctrl)
        handler._on_message(None, None, SimpleNamespace(topic=cfg.mqtt_command_topic, payload=b'ON', retain=True))
        self.assertEqual(ctrl._tx.queries, 0)
        self.assertEqual(ctrl._tx.sent, [])
    def test_live_numeric_command_gets_result(self):
        ctrl = controller(1)
        cfg = Config(mqtt_broker_host='unused.invalid')
        handler = MQTTHandler(cfg, ctrl)
        handler.client = Mock()
        ctrl.on_state_change = handler.on_state_change
        handler._on_message(None, None, SimpleNamespace(topic=cfg.mqtt_command_topic, payload=b'1', retain=False))
        self.assertEqual(ctrl.power, 'ON')
        self.assertEqual(ctrl._tx.sent, [(0, bytes([cec.IMAGE_VIEW_ON]))])
        results = [c.kwargs['payload'] for c in handler.client.publish.call_args_list if c.kwargs['topic'].endswith('/RESULT')]
        self.assertEqual(results, ['{"POWER": "ON"}'])

if __name__ == '__main__':
    unittest.main(verbosity=2)
