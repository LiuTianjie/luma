import unittest
from unittest.mock import Mock

from luma.errors import LumaError
from luma.nomad_render import _resource_values, resource_policy_warnings
from luma.control.server import _emit_resource_policy_warnings


class ResourcePolicyTests(unittest.TestCase):
    def test_two_peak_four_cpu_tasks_reserve_only_their_shares(self):
        resource = {'limits': {'cpus': '4', 'memory': '6G'}, 'reservations': {'cpus': '.5', 'memory': '1G'}}
        a, b = _resource_values(resource), _resource_values(resource)
        self.assertEqual(a['CPU'] + b['CPU'], 1000)
        self.assertEqual(a['MemoryMB'] + b['MemoryMB'], 2048)
        self.assertEqual(a['MemoryMaxMB'], 6144)

    def test_limit_only_does_not_reserve_the_peak(self):
        self.assertEqual(_resource_values({'limits': {'cpus': '8', 'memory': '6G'}}),
                         {'CPU': 100, 'MemoryMB': 256, 'MemoryMaxMB': 6144})

    def test_cpu_reservation_only_and_memory_limit(self):
        self.assertEqual(_resource_values({'reservations': {'cpus': '.5'}, 'limits': {'memory': '6G'}}),
                         {'CPU': 500, 'MemoryMB': 256, 'MemoryMaxMB': 6144})

    def test_small_memory_limit_clamps_default_reservation(self):
        self.assertEqual(_resource_values({'limits': {'memory': '64M'}}),
                         {'CPU': 100, 'MemoryMB': 64, 'MemoryMaxMB': 64})

    def test_unspecified_retains_bounded_memory_default(self):
        self.assertEqual(_resource_values({}), {'CPU': 100, 'MemoryMB': 256})

    def test_reservation_without_memory_limit_retains_legacy_bound(self):
        self.assertEqual(_resource_values({'reservations': {'memory': '512M'}})['MemoryMB'], 512)

    def test_memory_reservation_exceeding_limit_rejected(self):
        with self.assertRaisesRegex(LumaError, 'must not exceed'):
            _resource_values({'reservations': {'memory': '2G'}, 'limits': {'memory': '1G'}})

    def test_invalid_cpu_rejected_even_if_legacy_limit(self):
        for field in ['limits', 'reservations']:
            for value in ['nan', 'inf', 0, -1, True, 'half']:
                with self.subTest(field=field, value=value), self.assertRaises(LumaError):
                    _resource_values({field: {'cpus': value}})

    def test_zero_memory_rejected(self):
        for field in ['limits', 'reservations']:
            with self.assertRaises(LumaError):
                _resource_values({field: {'memory': '0M'}})

    def test_warning_explicitly_disclaims_legacy_cpu_ceiling(self):
        result = resource_policy_warnings({'limits': {'cpus': '8'}, 'reservations': {'cpus': '.5'}})
        self.assertIn('not enforced', result[0])
        self.assertIn('500 MHz', result[0])
        self.assertEqual(resource_policy_warnings({'reservations': {'cpus': '.5'}}), [])

    def test_warning_reaches_deployment_progress(self):
        steps, progress = [], Mock()
        _emit_resource_policy_warnings(steps, progress, 'worker', {'limits': {'cpus': '8'}})
        self.assertEqual(steps[0]['status'], 'warning')
        self.assertIn('100 MHz', steps[0]['message'])
        progress.assert_called_once_with(steps[0])
