"""MAIN network/bootstrap plan and ownership checks; fake Docker and routes only."""

import contextlib
import copy
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location('main_network_prep', Path(__file__).with_name('bootstrap_networks.py'))
prep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prep)


def limits():
    return {app: {'cpus': 0.5, 'memoryMb': 256, 'pids': 128} for app in ('frontend', 'backend', 'agent')}


def topology():
    value = {'version': 2, 'data': {'network': 'ieum_main_net_data', 'subnet': '10.60.10.0/24',
                                  'stores': {s: 'ieum-main-' + s for s in ('postgres', 'redis', 'mongo')}}}
    for color, block in [('blue', '10.40.10'), ('green', '10.50.10')]:
        directory = prep.DEPLOY_HOME / 'runtime' / color
        value[color] = {'network': 'ieum_main_net_' + color, 'subnet': block + '.0/24',
                        'containerName': 'ieum-main-' + color + '-frontend',
                        'ip': block + '.10', 'backendIp': block + '.11', 'agentIp': block + '.14',
                        'envDir': str(directory), 'envFile': str(directory / 'frontend.env')}
    return value


def bridge(name='bridge', subnet='172.17.0.0/16', identity='a' * 64, owned=False):
    return {'Id': identity, 'Name': name, 'Driver': 'bridge', 'Internal': False,
            'IPAM': {'Config': [{'Subnet': subnet}]}, 'Options': {}, 'Containers': {},
            'Labels': {prep.ENVIRONMENT_LABEL: 'main'} if owned else {}}


def routes():
    return [{'dst': 'default', 'dev': 'eth0'}, {'dst': '192.168.0.0/24', 'dev': 'eth0'}]


class TopologyTests(unittest.TestCase):
    def test_exact_main_networks_and_private_nonoverlapping_subnets_are_accepted(self):
        actual = prep.validate_topology(topology())
        self.assertEqual(set(actual), {'blue', 'green', 'data'})

    def test_dev_names_overlapping_public_or_noncanonical_subnets_and_paths_are_rejected(self):
        mutations = [lambda d: d['blue'].update(network='ieum_net_blue'),
                     lambda d: d['green'].update(subnet=d['blue']['subnet']),
                     lambda d: d['data'].update(subnet='8.8.8.0/24'),
                     lambda d: d['data'].update(subnet='10.60.10.5/24'),
                     lambda d: d['data']['stores'].update(postgres='ieum-postgres'),
                     lambda d: d['blue'].update(containerName='ieum-blue-frontend'),
                     lambda d: d['blue'].update(containerName='ieum-main-green-frontend'),
                     lambda d: d['blue'].update(containerName='ieum-main-custom-frontend'),
                     lambda d: d['blue'].update(ip='10.50.10.10'),
                     lambda d: d['blue'].update(ip='10.40.10.1'),
                     lambda d: d['blue'].update(envDir='/home/kwung/deploy/bluegreen/runtime/blue'),
                     lambda d: d['blue'].update(envFile='/etc/passwd')]
        for mutation in mutations:
            value = topology()
            mutation(value)
            with self.subTest(mutation=mutation), self.assertRaises(prep.PreparationError):
                prep.validate_topology(value)

    def test_app_resource_limits_match_controller_contract_without_defaults(self):
        data = prep.validate_limits(limits())
        self.assertEqual(data.count(b'\n'), 1)
        self.assertTrue(data.startswith(b'LIMITS_JSON='))
        self.assertEqual(json.loads(data.decode().split('=', 1)[1]), limits())
        for key, invalids in {'cpus': [True, 0, -1, 65, float('inf'), float('nan'), '1'],
                             'memoryMb': [True, 63, 262145, 1.5, '256'],
                             'pids': [True, 63, 16385, 2.5, '128']}.items():
            for invalid in invalids:
                value = limits()
                value['backend'][key] = invalid
                with self.subTest(key=key, value=invalid), self.assertRaises(prep.PreparationError):
                    prep.validate_limits(value)
        value = limits()
        value.pop('frontend')
        with self.assertRaises(prep.PreparationError):
            prep.validate_limits(value)


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.wanted = prep.validate_topology(topology())

    def test_existing_dev_docker_network_lan_and_vpn_conflicts_are_rejected(self):
        cases = [([bridge(subnet='10.40.0.0/16')], routes()),
                 ([bridge()], routes() + [{'dst': '10.50.0.0/16', 'dev': 'wg0'}]),
                 ([bridge()], routes() + [{'dst': '10.60.10.0/24', 'dev': 'eth0'}])]
        for networks, host_routes in cases:
            with self.subTest(routes=host_routes), self.assertRaises(prep.PreparationError):
                prep.validate_inventory(self.wanted, networks, host_routes)

    def test_owned_exact_main_networks_and_their_own_kernel_routes_can_be_reused(self):
        owned = bridge('ieum_main_net_blue', '10.40.10.0/24', 'b' * 64, True)
        host_routes = routes() + [{'dst': '10.40.10.0/24', 'dev': 'br-' + 'b' * 12},
                                  {'dst': '10.40.10.1', 'dev': 'br-' + 'b' * 12, 'type': 'local'}]
        self.assertEqual(set(prep.validate_inventory(self.wanted, [bridge(), owned], host_routes)), {'blue'})
        with self.assertRaises(prep.PreparationError):
            prep.validate_inventory(self.wanted, [bridge(), owned], host_routes + [{'dst': '10.40.0.0/16', 'dev': 'br-' + 'b' * 12}])

    def test_existing_main_names_are_not_adopted_without_owner_or_exact_subnet(self):
        mutations = [lambda n: n.update(Labels={}), lambda n: n.update(Driver='overlay'),
                     lambda n: n.update(Internal=True),
                     lambda n: n['IPAM']['Config'][0].update(Subnet='10.99.0.0/24'),
                     lambda n: n['Containers'].update(foreign={'Name': 'ieum-backend'})]
        for mutation in mutations:
            owned = bridge('ieum_main_net_blue', '10.40.10.0/24', 'b' * 64, True)
            mutation(owned)
            with self.subTest(mutation=mutation), self.assertRaises(prep.PreparationError):
                prep.validate_inventory(self.wanted, [bridge(), owned], routes())

    def test_failed_or_invalid_inventory_is_not_interpreted_as_absence(self):
        for result in ('', 'bad-id\n'):
            with patch.object(prep, 'run', return_value=result), self.assertRaises(prep.PreparationError):
                prep.inspect_networks()
        with patch.object(prep, 'run', side_effect=prep.PreparationError('docker_failure')):
            with self.assertRaises(prep.PreparationError): prep.inspect_networks()
        with patch.object(prep, 'run', return_value='[]'), self.assertRaises(prep.PreparationError):
            prep.inspect_routes()


class PreparationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.root.chmod(0o700)
        self.home_patch = patch.object(prep, 'DEPLOY_HOME', self.root / 'bluegreen-main')
        self.infra_patch = patch.object(prep, 'INFRA_HOME', self.root / 'ieum-infra-main')
        self.home_patch.start(); self.infra_patch.start()
        self.addCleanup(self.home_patch.stop); self.addCleanup(self.infra_patch.stop)
        self.settings = self.root / 'frontend-deployment.json'
        self.resources = self.root / 'resources.json'
        self.settings.write_text(json.dumps(topology()))
        self.resources.write_text(json.dumps(limits()))
        self.wanted = prep.validate_topology(topology())
        self.resource_bytes = prep.validate_limits(limits())

    def test_default_plan_performs_no_file_or_network_writes(self):
        before = set(self.root.rglob('*'))
        output = io.StringIO()
        with patch.object(prep.pwd, 'getpwuid', return_value=SimpleNamespace(pw_name='kwung')), \
                patch.object(prep, 'inspect_networks', return_value=[bridge()]), \
                patch.object(prep, 'inspect_routes', return_value=routes()), \
                patch.object(prep, 'apply') as apply, contextlib.redirect_stdout(output):
            self.assertEqual(prep.main(['--settings-file', str(self.settings), '--resource-limits', str(self.resources)]), 0)
        apply.assert_not_called()
        self.assertEqual(before, set(self.root.rglob('*')))
        self.assertEqual(json.loads(output.getvalue())['operation'], 'plan')

    def test_root_is_refused_even_for_plan(self):
        with patch.object(prep.os, 'geteuid', return_value=0), contextlib.redirect_stderr(io.StringIO()), \
                patch.object(prep, 'run') as command:
            self.assertEqual(prep.main(['--settings-file', str(self.settings), '--resource-limits', str(self.resources)]), 1)
        command.assert_not_called()

    def test_explicit_apply_creates_only_main_and_reuses_without_modifying_existing(self):
        inventory = [bridge()]
        commands = []
        def run(args):
            commands.append(args)
            self.assertEqual(args[:3], ['docker', 'network', 'create'])
            role = next(k for k, v in prep.EXPECTED_NAMES.items() if v == args[-1])
            identity = {'blue': 'b', 'green': 'c', 'data': 'd'}[role] * 64
            inventory.append(bridge(args[-1], args[args.index('--subnet') + 1], identity, True))
            return identity
        with patch.object(prep, 'inspect_networks', side_effect=lambda: copy.deepcopy(inventory)), \
                patch.object(prep, 'inspect_routes', return_value=routes()), patch.object(prep, 'run', side_effect=run):
            prep.apply(self.wanted, self.resource_bytes)
            self.assertEqual(len(commands), 3)
            target = prep.DEPLOY_HOME / 'control/resource-limits.env'
            before = target.stat().st_mtime_ns
            prep.apply(self.wanted, self.resource_bytes)
        self.assertEqual(len(commands), 3)
        self.assertEqual(target.stat().st_mtime_ns, before)
        self.assertEqual(target.read_bytes(), self.resource_bytes)
        for directory in prep.directories(): self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        for path in (target, prep.DEPLOY_HOME / 'deploy.lock'):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_unsafe_directory_resource_pin_and_symlink_are_never_repaired(self):
        prep.DEPLOY_HOME.mkdir(mode=0o755)
        with self.assertRaises(prep.PreparationError): prep.validate_paths(self.resource_bytes)
        self.assertEqual(stat.S_IMODE(prep.DEPLOY_HOME.stat().st_mode), 0o755)
        prep.DEPLOY_HOME.chmod(0o700)
        control = prep.DEPLOY_HOME / 'control'
        control.mkdir(mode=0o700)
        pin = control / 'resource-limits.env'
        pin.write_bytes(b'LIMITS_JSON=existing\n'); pin.chmod(0o600)
        with self.assertRaises(prep.PreparationError): prep.validate_paths(self.resource_bytes)
        self.assertEqual(pin.read_bytes(), b'LIMITS_JSON=existing\n')
        pin.unlink(); pin.symlink_to(self.resources)
        with self.assertRaises(OSError): prep.validate_paths(self.resource_bytes)

    def test_busy_main_lock_stops_network_creation(self):
        for path in prep.directories(): path.mkdir(mode=0o700)
        lock_path = prep.DEPLOY_HOME / 'deploy.lock'
        lock_path.touch(mode=0o600)
        with lock_path.open('r+') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(prep, 'inspect_networks', return_value=[bridge()]), \
                    patch.object(prep, 'inspect_routes', return_value=routes()), patch.object(prep, 'run') as command, \
                    self.assertRaises(prep.PreparationError):
                prep.apply(self.wanted, self.resource_bytes)
        command.assert_not_called()


if __name__ == '__main__':
    unittest.main()
