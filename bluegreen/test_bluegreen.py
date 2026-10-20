import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('bg', Path(__file__).with_name('bluegreen.py'))
bg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bg)

class DeploymentSafetyTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home_patch = patch.object(bg, 'HOME', Path(self.directory.name))
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        bg.save({'active': 'green', 'previous': 'blue', 'slots': {}})
        bg.atomic(bg.HOME / 'control/active', 'green\n')

    def test_unhealthy_candidate_never_changes_routing(self):
        with patch.object(bg, 'check', side_effect=RuntimeError('unhealthy')), patch.object(bg, 'route') as route:
            with self.assertRaises(RuntimeError): bg.switch('blue')
            route.assert_not_called()
        self.assertEqual(bg.state()['active'], 'green')

    def test_failed_ingress_probe_restores_previous_route_and_owner(self):
        with patch.object(bg, 'check'), patch.object(bg, 'control'), patch.object(bg, 'route') as route, \
                patch.object(bg, 'verify_ingress', side_effect=RuntimeError('TLS probe failed')):
            with self.assertRaises(RuntimeError): bg.switch('blue')
            self.assertEqual([call.args[0] for call in route.call_args_list], ['blue', 'green'])
        self.assertEqual(bg.state()['active'], 'green')
        self.assertEqual((bg.HOME / 'control/active').read_text().strip(), 'green')

    def test_active_environment_cannot_be_replaced(self):
        with patch.object(bg, 'docker') as docker:
            with self.assertRaises(RuntimeError): bg.remove_inactive('green')
            docker.assert_not_called()

    def test_drain_timeout_resumes_previous_environment_without_stopping_it(self):
        with patch.object(bg, 'exists', return_value=True), \
             patch.object(bg, 'inspect', return_value={'State': {'Status': 'running'}}), \
             patch.object(bg, 'control') as control, patch.object(bg, 'docker') as docker:
            with self.assertRaises(RuntimeError): bg.drain('blue', timeout=0)
            self.assertEqual([c.args for c in control.call_args_list], [('blue', 'drain'), ('blue', 'resume')])
            docker.assert_not_called()

if __name__ == '__main__': unittest.main()
