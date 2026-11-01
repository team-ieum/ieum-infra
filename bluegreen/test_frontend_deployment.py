"""Synthetic frontend topology, routing and rollback settings; no Docker or root access."""
import copy
import contextlib
import io
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


def module(name, file):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(file))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


helper = module('frontend_nginx_helper', 'nginx-helper.py')
bg = module('frontend_controller', 'bluegreen.py')


def policy():
    result = {'version': 1, 'network': 'custom_network', 'subnet': '10.10.10.0/24'}
    for color, ip in [('blue', '10.10.10.40'), ('green', '10.10.10.50')]:
        directory = '/home/kwung/deploy/bluegreen/runtime/' + color + '/custom'
        result[color] = {'containerName': 'custom-' + color + '-frontend', 'ip': ip,
                         'envDir': directory, 'envFile': directory + '/frontend.env'}
    return result


class FrontendPolicyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for attribute, value in [('FRONTEND_POLICY', self.root / 'policy.json'),
                ('FRONTEND_CANDIDATE', self.root / 'candidate.json'), ('DESTINATION', self.root / 'route.conf')]:
            p = patch.object(helper, attribute, value); p.start(); self.addCleanup(p.stop)

    def private(self, content=None):
        helper.FRONTEND_CANDIDATE.write_text(helper.policy_text(policy()) if content is None else content)
        helper.FRONTEND_CANDIDATE.chmod(0o600)
        return helper.FRONTEND_CANDIDATE

    def test_explicit_names_addresses_paths_are_accepted(self):
        self.assertEqual(helper.validate_frontend_settings(policy()), policy())
        alternate = policy(); alternate['blue']['envFile'] = alternate['blue']['envDir'] + '/.env'
        self.assertEqual(helper.validate_frontend_settings(alternate), alternate)

    def test_invalid_types_duplicate_names_ips_and_reserved_app_identities_are_rejected(self):
        variants = []
        for key, value in [('version', True), ('network', 'host'), ('network', 'name;cmd'),
                           ('subnet', '10.10.10.1/24'), ('subnet', '192.168.0.0/24')]:
            data = policy(); data[key] = value; variants.append(data)
        for key, value in [('containerName', 'ieum-blue-backend'), ('containerName', 'ieum-frontend'),
                           ('containerName', 'custom-green-frontend'), ('containerName', '-invalid'),
                           ('ip', '10.10.10.21'), ('ip', '10.10.10.50'), ('ip', '10.10.10.0'),
                           ('ip', '10.10.10.255'), ('ip', '::1'), ('ip', '10.10.10.040'),
                           ('envDir', '/etc'), ('envFile', '/etc/passwd'),
                           ('envFile', '/home/kwung/deploy/bluegreen/runtime/blue/custom/backend.env')]:
            data = policy(); data['blue'][key] = value; variants.append(data)
        data = policy(); data['unknown'] = 'no'; variants.append(data)
        data = policy(); data['blue']['extra'] = 'no'; variants.append(data)
        for data in variants:
            with self.subTest(data=data), self.assertRaises(ValueError):
                helper.validate_frontend_settings(data)

    def test_private_policy_reader_rejects_duplicate_keys_and_untrusted_files(self):
        source = self.private()
        self.assertEqual(helper.read_frontend_file(source, owner=os.geteuid(), mode=0o600), policy())
        with self.assertRaises(ValueError):
            helper.read_frontend_file(source, owner=os.geteuid() + 1, mode=0o600)
        source.chmod(0o644)
        with self.assertRaises(ValueError):
            helper.read_frontend_file(source, owner=os.geteuid(), mode=0o600)
        source.chmod(0o600)
        link = self.root / 'link'; link.symlink_to(source)
        with self.assertRaises(ValueError):
            helper.read_frontend_file(link, owner=os.geteuid(), mode=0o600)
        link.unlink(); os.link(source, link)
        with self.assertRaises(ValueError):
            helper.read_frontend_file(source, owner=os.geteuid(), mode=0o600)
        link.unlink()
        self.private(helper.policy_text(policy()).replace('"version":1', '"version":1,"version":1'))
        with self.assertRaises(ValueError):
            helper.read_frontend_file(source, owner=os.geteuid(), mode=0o600)

    def test_fifo_and_oversized_files_are_rejected_without_blocking(self):
        source = self.root / 'fifo'; os.mkfifo(source, 0o600)
        with self.assertRaises(ValueError):
            helper.read_frontend_file(source, owner=os.geteuid(), mode=0o600)
        source = self.private('x' * (helper.MAX_POLICY_BYTES + 1))
        with self.assertRaises(ValueError):
            helper.read_frontend_file(source, owner=os.geteuid(), mode=0o600)

    def test_route_uses_custom_frontend_address_for_both_colors(self):
        with patch.object(helper, 'frontend_policy', return_value=policy()):
            for color in ('blue', 'green'):
                rendered = helper.render(color)
                self.assertIn('upstream ieum_frontend { server ' + policy()[color]['ip'] + ':80; }', rendered)
                self.assertIn('server ' + helper.ROUTES[color][1] + ':8080;', rendered)
            self.assertIn('server 10.10.10.10:80;', helper.render('legacy'))

    def test_invalid_policy_never_writes_or_reloads_nginx(self):
        helper.FRONTEND_POLICY.write_text('invalid'); helper.FRONTEND_POLICY.chmod(0o644)
        with patch.object(helper, 'atomic_write') as write, patch.object(helper.subprocess, 'run') as reload:
            with self.assertRaises(ValueError): helper.apply('blue')
        write.assert_not_called(); reload.assert_not_called()

    def test_configuration_is_immutable_and_idempotent(self):
        source = self.private()
        with patch.object(helper.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=os.geteuid())), \
             patch.object(helper, 'frontend_policy', return_value=policy()), patch.object(helper, 'atomic_write') as write:
            helper.configure_frontend()
            changed = policy(); changed['blue']['ip'] = '10.10.10.60'; source.write_text(helper.policy_text(changed))
            with self.assertRaisesRegex(ValueError, 'pinned'): helper.configure_frontend()
        write.assert_not_called()

    def test_initial_configuration_requires_legacy_and_installs_only_validated_json(self):
        self.private()
        helper.DESTINATION.write_text('# Managed by ieum-bluegreen-nginx. active=blue\n')
        with patch.object(helper.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=os.geteuid())), \
             patch.object(helper, 'frontend_policy', return_value=None), patch.object(helper.os, 'chown') as chown:
            with self.assertRaisesRegex(ValueError, 'before Blue/Green'): helper.configure_frontend()
            self.assertFalse(helper.FRONTEND_POLICY.exists())
            helper.DESTINATION.write_text('# Managed by ieum-bluegreen-nginx. active=legacy\n')
            with self.assertRaisesRegex(ValueError, 'separated-network'): helper.configure_frontend()
        self.assertFalse(helper.FRONTEND_POLICY.exists()); chown.assert_not_called()

    def test_missing_pin_cannot_silently_restore_hardcoded_addresses(self):
        marker = self.root / 'control/frontend-deployment-sha256'
        marker.parent.mkdir(); marker.write_text('pinned')
        with self.assertRaisesRegex(ValueError, 'missing'): helper.render('blue')
    def test_deployment_directories_do_not_overlap_other_app_snapshot_files(self):
        root = helper.RUNTIME_ROOT / 'blue'
        for suffix in ('backend.env', 'agent.env', 'a.b', '.hidden', 'custom/x.y'):
            value = policy(); directory = root / suffix
            value['blue'].update(envDir=str(directory), envFile=str(directory / 'frontend.env'))
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                helper.validate_frontend_settings(value)
        for suffix in ('', 'custom', 'custom/sub'):
            value = policy(); directory = root / suffix
            value['blue'].update(envDir=str(directory), envFile=str(directory / 'frontend.env'))
            self.assertEqual(helper.validate_frontend_settings(value), value)


class FrontendControllerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for attribute, value in [('HOME', self.root), ('FRONTEND_POLICY', self.root / 'root-policy'),
                ('FRONTEND_SETTINGS', None), ('IPS', copy.deepcopy(bg.IPS))]:
            p = patch.object(bg, attribute, value); p.start(); self.addCleanup(p.stop)
        self.value = policy()

    def install(self):
        bg.FRONTEND_POLICY.write_text('root-owned policy is simulated by mocked helper')
        with patch.object(bg, 'run', return_value=helper.policy_text(self.value)):
            bg.load_frontend_settings()

    def test_all_commands_reuse_same_custom_names_ips_and_snapshot_paths(self):
        self.install()
        for color in ('blue', 'green'):
            self.assertEqual(bg.name(color, 'frontend'), self.value[color]['containerName'])
            self.assertEqual(bg.IPS[color]['frontend'], self.value[color]['ip'])
            self.assertEqual(bg.runtime_env_file(color, 'frontend'), Path(self.value[color]['envFile']))
            self.assertEqual(bg.name(color, 'backend'), 'ieum-' + color + '-backend')
        self.assertEqual(bg.name('legacy', 'frontend'), 'ieum-frontend')

    def test_secrets_mismatch_is_rejected_before_state_or_docker_changes(self):
        self.install()
        other = copy.deepcopy(self.value); other['blue']['ip'] = '10.10.10.60'
        with patch.object(bg, 'run', side_effect=[helper.policy_text(self.value), helper.policy_text(other)]), \
             patch.object(bg, 'atomic') as write, patch.object(bg, 'docker') as docker:
            with self.assertRaisesRegex(RuntimeError, 'differ'): bg.load_frontend_settings('private-candidate')
        write.assert_not_called(); docker.assert_not_called()

    def test_missing_root_pin_and_changed_fingerprint_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'Configure frontend'): bg.load_frontend_settings('candidate')
        bg.atomic(bg.HOME / 'control/frontend-deployment-sha256', 'pinned\n')
        with self.assertRaisesRegex(RuntimeError, 'fallback'): bg.load_frontend_settings()
        bg.FRONTEND_POLICY.write_text('simulated root file')
        with patch.object(bg, 'run', return_value=helper.policy_text(self.value)):
            with self.assertRaisesRegex(RuntimeError, 'changed'): bg.load_frontend_settings()

    def test_runtime_writes_and_reuse_use_selected_path_and_preserve_other_color(self):
        self.value = copy.deepcopy(self.value)
        for color in ('blue', 'green'):
            directory = self.root / 'runtime' / color / 'custom'
            self.value[color]['envDir'] = str(directory)
            self.value[color]['envFile'] = str(directory / '.env')
        bg.FRONTEND_SETTINGS = self.value
        blue = bg.write_runtime_env('blue', 'frontend', 'VITE_API_URL=https://old.invalid\n')
        green = bg.write_runtime_env('green', 'frontend', 'VITE_API_URL=https://new.invalid\n')
        bg.write_runtime_env('blue', 'frontend', 'VITE_API_URL=https://next.invalid\n')
        self.assertEqual(bg.env_values('frontend', {}, 'green')['VITE_API_URL'], 'https://new.invalid')
        self.assertEqual(green.read_text(), 'VITE_API_URL=https://new.invalid\n')
        self.assertEqual(blue.stat().st_mode & 0o777, 0o600)

    def test_symlink_runtime_directory_is_rejected_before_writing(self):
        self.value['blue']['envFile'] = str(self.root / 'runtime/blue/custom/frontend.env')
        bg.FRONTEND_SETTINGS = self.value
        directory = self.root / 'runtime/blue'; directory.mkdir(parents=True, mode=0o700)
        (self.root / 'runtime').chmod(0o700)
        target = self.root / 'other'; target.mkdir()
        (directory / 'custom').symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'directory'): bg.write_runtime_env('blue', 'frontend', 'VALUE=x\n')
        self.assertFalse((target / 'frontend.env').exists())

    def test_network_name_subnet_driver_and_gateway_are_checked(self):
        self.install()
        good = [{'Driver': 'bridge', 'IPAM': {'Config': [{'Subnet': '10.10.10.0/24', 'Gateway': '10.10.10.1'}]}}]
        with patch.object(bg, 'docker', return_value=json.dumps(good)) as docker:
            bg.validate_frontend_network()
            docker.assert_called_once_with('network', 'inspect', 'custom_network')
        for changed in ([{'Driver': 'host', 'IPAM': good[0]['IPAM']}],
                [{'Driver': 'bridge', 'IPAM': {'Config': [{'Subnet': '10.10.0.0/16'}]}}],
                [{'Driver': 'bridge', 'IPAM': {'Config': [{'Subnet': '10.10.10.0/24', 'Gateway': '10.10.10.40'}]}}], []):
            with patch.object(bg, 'docker', return_value=json.dumps(changed)):
                with self.assertRaisesRegex(RuntimeError, 'network'): bg.validate_frontend_network()

    def test_configure_refuses_existing_slots_without_touching_root_or_docker(self):
        bg.save({'active': 'legacy', 'slots': {'blue': {}}})
        with patch.object(bg, 'run', return_value=helper.policy_text(self.value)) as run, patch.object(bg, 'docker') as docker:
            with self.assertRaisesRegex(RuntimeError, 'slots'): bg.configure_frontend_settings('candidate')
        self.assertEqual(run.call_count, 1); docker.assert_not_called()

    def test_old_schema_configuration_requires_explicit_network_migration(self):
        route = self.root / 'route.conf'; route.write_text('# Managed by ieum-bluegreen-nginx. active=legacy\n')
        with patch.object(bg, 'ROUTE_CONFIG', route), patch.object(bg, 'run', return_value=helper.policy_text(self.value)), \
             patch.object(bg, 'docker', return_value=''), patch.object(bg, 'atomic') as write:
            with self.assertRaisesRegex(RuntimeError, 'separated Blue/Green'):
                bg.configure_frontend_settings('candidate')
        write.assert_not_called()

    def test_backend_deployment_also_requires_the_shared_topology_before_prepare(self):
        with patch.object(bg.sys, 'argv', ['bluegreen', 'deploy', '--service', 'backend', '--image', 'test']), \
             patch.object(bg, 'prepare') as prepare:
            with self.assertRaisesRegex(RuntimeError, 'Configure frontend deployment Secrets'): bg.main()
        prepare.assert_not_called()

    def test_frontend_deploy_requires_explicit_metadata_before_prepare(self):
        with patch.object(bg.sys, 'argv', ['bluegreen', 'deploy', '--service', 'frontend', '--image', 'test']), \
             patch.object(bg, 'prepare') as prepare:
            with self.assertRaisesRegex(RuntimeError, 'requires explicit'): bg.main()
        prepare.assert_not_called()


class FrontendPreflightTest(unittest.TestCase):
    def setUp(self):
        FrontendControllerTest.setUp(self)
        for color in ('blue', 'green'):
            directory = self.root / 'runtime' / color / 'custom'
            self.value[color].update(envDir=str(directory), envFile=str(directory / 'frontend.env'))
        bg.FRONTEND_SETTINGS = self.value
        bg.save({'active': 'green', 'previous': None, 'slots': {}})
        self.network = [{'Driver': 'bridge', 'IPAM': {'Config': [{'Subnet': '10.10.10.0/24', 'Gateway': '10.10.10.1'}]}}]

    def prepare_rejected(self):
        before = (self.root / 'state.json').read_bytes()
        with patch.object(bg, 'assert_consistent'), patch.object(bg, 'docker', return_value=json.dumps(self.network)), \
             patch.object(bg, 'remove_inactive') as remove, patch.object(bg, 'capture_legacy_snapshot') as capture:
            with self.assertRaises(RuntimeError): bg.prepare('blue', {})
        remove.assert_not_called(); capture.assert_not_called()
        self.assertEqual((self.root / 'state.json').read_bytes(), before)
        self.assertFalse((self.root / 'control/pending-recovery-not-before').exists())
        self.assertFalse((self.root / 'control/backend-time-zone').exists())

    def configure_rejected(self):
        bg.save({'active': 'legacy', 'slots': {}})
        def docker(*args):
            return '' if args[0] == 'ps' else json.dumps(self.network)
        with patch.object(bg, 'run', return_value=helper.policy_text(self.value)) as run, \
             patch.object(bg, 'docker', side_effect=docker), patch.object(bg, 'route_color', return_value='legacy'):
            with self.assertRaises(RuntimeError): bg.configure_frontend_settings('synthetic-candidate')
        self.assertFalse(any(call.args[0] == 'sudo' for call in run.call_args_list))
        self.assertFalse((self.root / 'frontend-deployment-candidate.json').exists())

    def test_public_runtime_directory_is_rejected_before_slot_removal_and_helper(self):
        (self.root / 'runtime').mkdir(mode=0o755)
        (self.root / 'runtime').chmod(0o755)
        self.prepare_rejected(); self.configure_rejected()
        self.assertEqual((self.root / 'runtime').stat().st_mode & 0o777, 0o755)

    def test_symlink_intermediate_directory_is_rejected_before_slot_removal(self):
        (self.root / 'runtime').symlink_to(self.root / 'not-created')
        self.prepare_rejected()

    def test_missing_directories_pass_and_are_created_privately_only_at_write(self):
        before = set(self.root.iterdir())
        bg.verify_frontend_runtime_path('blue')
        self.assertEqual(set(self.root.iterdir()), before)
        path = bg.write_runtime_env('blue', 'frontend', 'VITE_API_URL=https://test.invalid\n')
        for directory in (self.root / 'runtime', self.root / 'runtime/blue', path.parent):
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_existing_file_links_modes_owner_and_home_are_checked_without_mutation(self):
        path = bg.write_runtime_env('blue', 'frontend', 'VALUE=preserve\n')
        path.chmod(0o644)
        with self.assertRaisesRegex(RuntimeError, 'runtime/blue/custom/frontend.env'):
            bg.verify_frontend_runtime_path('blue')
        path.chmod(0o600)
        linked = self.root / 'alias'; os.link(path, linked)
        with self.assertRaises(RuntimeError): bg.verify_frontend_runtime_path('blue')
        linked.unlink()
        with patch.object(bg.os, 'geteuid', return_value=os.geteuid() + 1):
            with self.assertRaises(RuntimeError): bg.verify_frontend_runtime_path('blue')
        self.root.chmod(0o755)
        with self.assertRaisesRegex(RuntimeError, 'account: \\.'): bg.verify_frontend_runtime_path('blue')
        self.root.chmod(0o700)
        self.assertEqual(path.read_text(), 'VALUE=preserve\n')

    def test_frontend_ip_occupied_by_infrastructure_blocks_prepare_and_configure(self):
        self.network[0]['Containers'] = {'a' * 64: {'Name': 'redis', 'IPv4Address': '10.10.10.40/24'}}
        self.prepare_rejected(); self.configure_rejected()

    def test_backend_and_agent_ip_occupancy_is_also_checked_for_target_color(self):
        for app in ('backend', 'agent'):
            self.network[0]['Containers'] = {'a' * 64: {'Name': 'unexpected', 'IPv4Address': bg.IPS['blue'][app] + '/24'}}
            self.prepare_rejected()

    def test_target_slot_own_endpoints_are_replaceable_during_prepare(self):
        self.network[0]['Containers'] = {'a' * 64: {'Name': bg.name('blue', 'frontend'), 'IPv4Address': '10.10.10.40/24'}}
        with patch.object(bg, 'assert_consistent'), patch.object(bg, 'docker', return_value=json.dumps(self.network)), \
             patch.object(bg, 'inspect', side_effect=RuntimeError('passed preflight')) as inspect, patch.object(bg, 'remove_inactive') as remove:
            with self.assertRaisesRegex(RuntimeError, 'passed preflight'): bg.prepare('blue', {})
        inspect.assert_called_once(); remove.assert_not_called()
        self.configure_rejected()

    def test_other_color_does_not_block_target_and_missing_endpoints_are_empty(self):
        self.network[0]['Containers'] = {'a' * 64: {'Name': 'other', 'IPv4Address': '10.10.10.50/24'}}
        with patch.object(bg, 'docker', return_value=json.dumps(self.network)):
            bg.validate_frontend_network(colors=('blue',))
            with self.assertRaises(RuntimeError): bg.validate_frontend_network()
        del self.network[0]['Containers']
        with patch.object(bg, 'docker', return_value=json.dumps(self.network)):
            bg.validate_frontend_network()

    def test_malformed_endpoint_addresses_fail_closed(self):
        for endpoint in ({'Name': 'other', 'IPv4Address': 'invalid'}, {'Name': 'other', 'IPv4Address': '::1/64'},
                {'Name': 'other'}, {'IPv4Address': '10.10.10.40/24'}):
            self.network[0]['Containers'] = {'a' * 64: endpoint}
            with patch.object(bg, 'docker', return_value=json.dumps(self.network)):
                with self.assertRaisesRegex(RuntimeError, 'network'): bg.validate_frontend_network()


class StaleSlotRetirementTest(unittest.TestCase):
    SECRET = 'synthetic-inspect-env-must-never-be-printed'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for attribute, value in [('HOME', self.root), ('FRONTEND_POLICY', self.root / 'policy.json'),
                ('ROUTE_CONFIG', self.root / 'nginx.conf'), ('FRONTEND_SETTINGS', None)]:
            p = patch.object(bg, attribute, value); p.start(); self.addCleanup(p.stop)
        self.infos = {}
        for color in ('blue', 'green'):
            for app in bg.APPS:
                identity = format(len(self.infos) + 1, '064x')
                self.infos[identity] = {'Id': identity, 'Name': '/ieum-' + color + '-' + app,
                    'Image': 'sha256:' + format(len(self.infos) + 100, '064x'),
                    'Config': {'Labels': {'ieum.bluegreen.managed': 'true', 'ieum.bluegreen.color': color},
                        'Env': ['PRIVATE_TOKEN=' + self.SECRET]},
                    'State': {'Running': False, 'Status': 'exited'}, 'HostConfig': {'RestartPolicy': {'Name': 'no'}}}
        bg.save({'active': 'legacy', 'previous': 'blue', 'slots': {color: {
            'status': 'retained', 'release': format(i + 1, '016x'), 'images': {}} for i, color in enumerate(('blue', 'green'))}})
        for path, content in [('control/active', 'legacy\n'), ('control/phase', 'legacy\n'),
                ('control/pending-recovery-not-before', '2026-10-20T00:00:00Z\n'), ('control/backend-time-zone', 'UTC\n'),
                ('snapshots/legacy.json', self.SECRET), ('lock-owner.json', self.SECRET), ('runtime/blue/frontend.env', self.SECRET), ('assets/old.js', 'preserve')]:
            bg.atomic(self.root / path, content)
        bg.atomic(bg.ROUTE_CONFIG, '# Managed by ieum-bluegreen-nginx. active=legacy\n')
        (self.root / 'deploy.lock').touch(mode=0o600)
        self.calls = []; self.rm_count = 0; self.fail_rm = None; self.ps_override = None
        self.network = [{'Driver': 'bridge', 'IPAM': {'Config': [{'Subnet': '10.10.10.0/24', 'Gateway': '10.10.10.1'}]}}]

    def files(self):
        return {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob('*') if path.is_file()}

    def docker(self, command, **kwargs):
        self.assertEqual(command[0], 'docker')
        self.assertEqual(kwargs['timeout'], bg.CANDIDATE_DOCKER_TIMEOUT)
        args = command[1:]; self.calls.append(args)
        if args[0] == 'ps':
            if self.ps_override is not None: return self.ps_override(command, kwargs)
            if '--filter' in args:
                filter = args[args.index('--filter') + 1]
                if filter.startswith('name='):
                    name = filter[len('name=^'):-1]
                    ids = [identity for identity, info in self.infos.items() if info['Name'] == name]
                else:
                    ids = [identity for identity, info in self.infos.items()
                           if info['Config']['Labels'].get('ieum.bluegreen.managed') == 'true']
            else: ids = list(self.infos)
            output = '\n'.join(ids)
        elif args[0] == 'inspect':
            self.assertRegex(args[1], r'\A[a-f0-9]{64}\Z')
            output = json.dumps([self.infos[args[1]]])
        elif args[0] == 'rm':
            self.assertEqual(len(args), 2); self.assertRegex(args[1], r'\A[a-f0-9]{64}\Z')
            self.rm_count += 1
            if self.rm_count == self.fail_rm:
                return SimpleNamespace(returncode=1, stdout='', stderr=self.SECRET)
            del self.infos[args[1]]; output = ''
        else: self.fail('Unexpected Docker mutation')
        return SimpleNamespace(returncode=0, stdout=output, stderr='')

    def invoke(self, confirmation=None):
        argv = ['bluegreen', '--lock-timeout', '0', 'retire-stale-slots']
        if confirmation is not None: argv += ['--confirm-plan', confirmation]
        output = io.StringIO()
        with patch.object(bg.sys, 'argv', argv), patch.object(bg.subprocess, 'run', side_effect=self.docker), \
             contextlib.redirect_stdout(output):
            result = bg.main()
        self.assertNotIn(self.SECRET, output.getvalue())
        return result, output.getvalue()

    def digest(self):
        result, output = self.invoke()
        self.assertEqual(result, 0)
        return json.loads(output)['planSha256']

    def assert_no_mutation(self, before):
        self.assertEqual(self.files(), before)
        self.assertEqual(self.rm_count, 0)
        self.assertTrue(all(args[0] in ('ps', 'inspect') for args in self.calls))

    def test_dry_run_is_byte_identical_and_plan_has_only_review_fields(self):
        before = self.files(); result, output = self.invoke()
        self.assertEqual(result, 0); self.assert_no_mutation(before)
        report = json.loads(output); plan = report['plan']
        self.assertEqual(len(plan['containers']), 6)
        self.assertEqual(set(plan), {'schemaVersion', 'containers', 'stateSlots'})
        self.assertEqual([(item['color'], item['app']) for item in plan['containers']],
                         sorted((color, app) for color in ('blue', 'green') for app in bg.APPS))
        for item in plan['containers']:
            self.assertEqual(set(item), {'color', 'app', 'name', 'id', 'image', 'status'})
        canonical = json.dumps(plan, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
        self.assertEqual(report['planSha256'], bg.hashlib.sha256(canonical.encode()).hexdigest())

    def test_confirmed_plan_removes_only_ids_archives_state_and_allows_configuration(self):
        before = self.files(); digest = self.digest()
        result, _ = self.invoke(digest)
        self.assertEqual(result, 0); self.assertEqual(self.rm_count, 6); self.assertEqual(self.infos, {})
        self.assertEqual(bg.state()['slots'], {}); self.assertIsNone(bg.state()['previous'])
        self.assertEqual(bg.state()['active'], 'legacy')
        archives = list((self.root / 'retired-slots').glob('*.json'))
        self.assertEqual(len(archives), 1); self.assertEqual(archives[0].read_bytes(), before['state.json'])
        self.assertEqual(archives[0].stat().st_mode & 0o777, 0o600)
        for file, content in before.items():
            if file != 'state.json': self.assertEqual((self.root / file).read_bytes(), content)
        history = (self.root / 'history.jsonl').read_text()
        self.assertNotIn(self.SECRET, history); self.assertEqual(json.loads(history)['action'], 'stale_slots_retired')
        removals = [args[1] for args in self.calls if args[0] == 'rm']
        self.assertEqual(removals, [format(i, '064x') for i in (3, 2, 1, 6, 5, 4)])
        value = policy()
        for color in ('blue', 'green'):
            directory = self.root / 'runtime' / color
            value[color].update(envDir=str(directory), envFile=str(directory / 'frontend.env'))
        # The legacy atomic-created runtime directory was outside this command's remit.
        # Its deliberate permission correction belongs to the separate operator preflight.
        for path in (self.root / 'runtime', self.root / 'runtime/blue'):
            path.chmod(0o700)
        def run(*args):
            if args[0] == 'python3': return helper.policy_text(value)
            if args[0] == 'docker': return '' if args[1] == 'ps' else json.dumps(self.network)
            if args[:3] == ('sudo', '-n', bg.NGINX_HELPER): return ''
            self.fail('Unexpected configuration operation')
        with patch.object(bg, 'INSTALLED_HOME', self.root), patch.object(bg, 'run', side_effect=run) as run_mock, \
             patch.object(bg, 'load_frontend_settings', return_value=value):
            with self.assertRaisesRegex(RuntimeError, 'separated Blue/Green'): bg.configure_frontend_settings('candidate')
        self.assertFalse((self.root / 'frontend-deployment-candidate.json').exists())

    def test_digest_mismatch_outputs_new_plan_without_changes(self):
        before = self.files(); result, output = self.invoke('0' * 64)
        self.assertEqual(result, 1); self.assertIn('planSha256', json.loads(output)); self.assert_no_mutation(before)

    def test_invalid_container_state_labels_restart_and_identity_prevent_all_removals(self):
        digest = self.digest(); original = copy.deepcopy(self.infos)
        variants = [('running', lambda info: info['State'].update(Running=True, Status='running')),
                    ('restarting', lambda info: info['State'].update(Status='restarting')),
                    ('labels', lambda info: info['Config'].update(Labels={})),
                    ('color', lambda info: info['Config']['Labels'].update({'ieum.bluegreen.color': 'wrong'})),
                    ('restart', lambda info: info['HostConfig']['RestartPolicy'].update(Name='unless-stopped')),
                    ('id', lambda info: info.update(Id='f' * 64))]
        for label, change in variants:
            with self.subTest(label=label):
                self.infos = copy.deepcopy(original); change(self.infos[format(1, '064x')])
                before = self.files()
                with self.assertRaises(RuntimeError): self.invoke(digest)
                self.assert_no_mutation(before)

    def test_unknown_managed_container_and_ambiguous_or_malformed_name_inventory_are_rejected(self):
        digest = self.digest(); original = copy.deepcopy(self.infos)
        extra = copy.deepcopy(next(iter(self.infos.values()))); extra.update(Id='f' * 64, Name='/custom-managed')
        self.infos['f' * 64] = extra
        with self.assertRaises(RuntimeError): self.invoke(digest)
        self.assertEqual(self.rm_count, 0)
        self.infos = original
        for output in (format(1, '064x') + '\n' + format(2, '064x'), 'invalid', format(1, '064x') + '\n\n'):
            before = self.files()
            self.ps_override = lambda *args, value=output: SimpleNamespace(returncode=0, stdout=value, stderr=self.SECRET)
            with self.assertRaises(RuntimeError): self.invoke(digest)
            self.assert_no_mutation(before)

    def test_failed_or_timed_out_docker_inventory_never_means_absence(self):
        digest = self.digest()
        for timeout in (False, True):
            def fail(command, kwargs):
                if timeout: raise subprocess.TimeoutExpired(command, 10, stderr=self.SECRET)
                return SimpleNamespace(returncode=1, stdout='', stderr=self.SECRET)
            self.ps_override = fail; before = self.files()
            with self.assertRaises(RuntimeError) as error: self.invoke(digest)
            self.assertNotIn(self.SECRET, str(error.exception)); self.assert_no_mutation(before)

    def test_pin_route_phase_state_and_unfinished_transaction_preconditions_block_removal(self):
        digest = self.digest()
        initial = (self.root / 'state.json').read_text()
        changed_state = json.loads(initial); changed_state['active'] = 'blue'
        variants = [(bg.FRONTEND_POLICY, '{}'), (self.root / 'control/frontend-deployment-sha256', 'pin'),
                    (self.root / 'state.json', json.dumps(changed_state)), (self.root / 'control/active', 'blue'),
                    (self.root / 'control/phase', 'ready'),
                    (bg.ROUTE_CONFIG, '# Managed by ieum-bluegreen-nginx. active=blue\n'),
                    (self.root / 'transaction.json', '{}')]
        for path, value in variants:
            with self.subTest(file=str(path.relative_to(self.root))):
                old = path.read_bytes() if path.exists() else None
                path.write_text(value); before = self.files()
                with self.assertRaises(RuntimeError): self.invoke(digest)
                self.assert_no_mutation(before)
                if old is None: path.unlink()
                else: path.write_bytes(old)

    def test_second_rm_failure_stops_and_preserves_state_with_sanitized_history(self):
        digest = self.digest(); original = (self.root / 'state.json').read_bytes(); self.fail_rm = 2
        with self.assertRaisesRegex(RuntimeError, 'state was preserved'): self.invoke(digest)
        self.assertEqual(self.rm_count, 2); self.assertEqual((self.root / 'state.json').read_bytes(), original)
        self.assertFalse((self.root / 'retired-slots').exists())
        history = (self.root / 'history.jsonl').read_text(); self.assertNotIn(self.SECRET, history)
        record = json.loads(history); self.assertEqual(record['action'], 'stale_slots_retire_failed')
        self.assertEqual(sum(row['removed'] for row in record['containers']), 1)
        self.assertEqual({row['reason'] for row in record['containers']}, {'removed', 'docker_operation_failed', 'not_attempted'})

    def test_container_is_reinspected_by_id_before_any_removal(self):
        digest = self.digest(); original = (self.root / 'state.json').read_bytes()
        inspected = {}; docker = self.docker
        def changed(command, **kwargs):
            if command[1] == 'inspect':
                identity = command[2]; inspected[identity] = inspected.get(identity, 0) + 1
                if identity == format(3, '064x') and inspected[identity] == 2:
                    self.infos[identity]['Image'] = 'sha256:' + 'f' * 64
            return docker(command, **kwargs)
        with patch.object(self, 'docker', side_effect=changed):
            with self.assertRaises(RuntimeError): self.invoke(digest)
        self.assertEqual(self.rm_count, 0)
        self.assertEqual((self.root / 'state.json').read_bytes(), original)
        self.assertNotIn(self.SECRET, (self.root / 'history.jsonl').read_text())

    def test_final_id_inventory_must_prove_planned_containers_absent(self):
        digest = self.digest(); original = (self.root / 'state.json').read_bytes(); docker = self.docker
        def still_present(command, **kwargs):
            if command[1] == 'ps' and '--filter' not in command:
                return SimpleNamespace(returncode=0, stdout=format(1, '064x'), stderr='')
            return docker(command, **kwargs)
        with patch.object(self, 'docker', side_effect=still_present):
            with self.assertRaises(RuntimeError): self.invoke(digest)
        self.assertEqual((self.root / 'state.json').read_bytes(), original)
        history = json.loads((self.root / 'history.jsonl').read_text())
        self.assertTrue(any(row['reason'] == 'still_present' for row in history['containers']))
        self.assertFalse((self.root / 'retired-slots').exists())

    def test_dangling_pin_symlinks_are_not_treated_as_absent(self):
        digest = self.digest()
        for path in (bg.FRONTEND_POLICY, self.root / 'control/frontend-deployment-sha256'):
            path.symlink_to(self.root / 'absent-target')
            with self.assertRaises(RuntimeError): self.invoke(digest)
            self.assertEqual(self.rm_count, 0); path.unlink()

    def test_dry_run_requires_existing_lock_and_invalid_confirmation_is_not_executed(self):
        (self.root / 'deploy.lock').unlink(); before = self.files()
        with self.assertRaises(RuntimeError): self.invoke()
        self.assert_no_mutation(before)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit): self.invoke('not-a-sha')
        self.assert_no_mutation(before)

    def test_archive_failure_is_sanitized_and_preserves_old_state(self):
        digest = self.digest(); original = (self.root / 'state.json').read_bytes(); atomic = bg.atomic
        def fail(path, content, *args):
            if path.parent.name == 'retired-slots': raise OSError(self.SECRET)
            return atomic(path, content, *args)
        with patch.object(bg, 'atomic', side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, 'finalization failed') as error: self.invoke(digest)
        self.assertNotIn(self.SECRET, str(error.exception))
        self.assertEqual((self.root / 'state.json').read_bytes(), original)
        self.assertNotIn(self.SECRET, (self.root / 'history.jsonl').read_text())

    def test_empty_container_inventory_can_retire_only_slots(self):
        self.infos.clear(); digest = self.digest(); original = (self.root / 'state.json').read_bytes()
        self.invoke(digest)
        self.assertEqual(self.rm_count, 0); self.assertEqual(bg.state()['slots'], {})
        self.assertEqual(next((self.root / 'retired-slots').iterdir()).read_bytes(), original)

    def test_absent_state_is_reported_as_null_and_no_state_is_created(self):
        (self.root / 'state.json').unlink()
        _, output = self.invoke(); report = json.loads(output)
        self.assertIsNone(report['plan']['stateSlots'])
        self.invoke(report['planSha256'])
        self.assertFalse((self.root / 'state.json').exists()); self.assertFalse((self.root / 'retired-slots').exists())



if __name__ == '__main__': unittest.main()
