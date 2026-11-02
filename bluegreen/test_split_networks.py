"""Separated app bridges and one shared datastore set. Every Docker call is simulated."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


bg = load('split_controller', 'bluegreen.py')
helper = load('split_root_helper', 'nginx-helper.py')


def policy(runtime=None):
    runtime = helper.RUNTIME_ROOT if runtime is None else runtime
    value = {'version': 2, 'data': {'network': 'ieum_net_data', 'subnet': '10.30.10.0/24',
        'stores': {'postgres': 'ieum-postgres', 'redis': 'ieum-redis', 'mongo': 'ieum-mongo'}}}
    for color, subnet in [('blue', '10.20.10'), ('green', '10.10.10')]:
        directory = runtime / color
        value[color] = {'network': 'ieum_net_' + color, 'subnet': subnet + '.0/24',
            'containerName': 'ieum-' + color + '-frontend', 'ip': subnet + '.20',
            'backendIp': subnet + '.21', 'agentIp': subnet + '.24',
            'envDir': str(directory), 'envFile': str(directory / 'frontend.env')}
    return value


def environment(app):
    if app == 'backend': return {'DB_URL': 'jdbc:postgresql://ieum-postgres:5432/ieum',
        'REDIS_HOST': 'ieum-redis', 'MONGO_URI': 'mongodb://ieum-mongo:27017/ieum'}
    if app == 'agent': return {'MONGODB_URL': 'mongodb://ieum-mongo:27017/ieum', 'BACKEND_BASE_URL': 'http://10.10.10.21:8080'}
    return {'VITE_API_URL': 'https://test.invalid', 'NODE_ENV': 'production'}


class DockerFixture:
    def __init__(self, settings):
        self.settings = settings; self.calls = []; self.connect_failure = False; self.api = '1.48 1.48'
        self.networks = {}; self.infos = {}
        for entry in [settings['blue'], settings['green'], settings['data']]:
            self.networks[entry['network']] = {'Name': entry['network'], 'Driver': 'bridge', 'Internal': False,
                'IPAM': {'Config': [{'Subnet': entry['subnet'], 'Gateway': entry['subnet'].split('0/')[0] + '1'}]}, 'Containers': {}}
        for index, (kind, name) in enumerate(settings['data']['stores'].items()):
            cid = format(index + 1, '064x'); ip = '10.30.10.' + str(index + 12)
            self.add(name, cid, 'sha256:' + kind, {}, {settings['data']['network']: {'IPAddress': ip, 'GwPriority': 0}}, 'running')

    def add(self, name, cid, image, env, attached, state):
        info = {'Id': cid, 'Name': '/' + name, 'Image': image, 'Config': {'Env': [k + '=' + v for k, v in env.items()],
            'Labels': {'ieum.bluegreen.managed': 'true', 'ieum.bluegreen.color': 'green' if '-green-' in name else 'blue'}},
            'State': {'Status': state}, 'HostConfig': {'RestartPolicy': {'Name': 'unless-stopped', 'MaximumRetryCount': 0}}, 'NetworkSettings': {'Networks': attached}}
        self.infos[name] = self.infos[cid] = info
        for network, entry in attached.items():
            self.networks[network]['Containers'][cid] = {'Name': name, 'IPv4Address': entry['IPAddress'] + '/24'}
        return info

    def inspect(self, target):
        if target not in self.infos: raise RuntimeError('simulated container unavailable')
        return self.infos[target]

    def docker(self, *args):
        self.calls.append(args)
        if args[0] == 'version': return self.api
        if args[0] == 'ps': return '\n'.join(sorted({v['Name'][1:] for v in self.infos.values()}))
        if args[:2] == ('network', 'inspect'): return json.dumps([self.networks[args[2]]])
        if args[:2] == ('image', 'inspect'):
            return json.dumps([{'Id': args[2], 'Config': {'Env': [], 'Labels': {'ieum.bluegreen.capable': 'true', 'ieum.bluegreen.protocol': '2'}}}])
        if args[0] == 'create':
            name = args[args.index('--name') + 1]; network = args[args.index('--network') + 1]
            ip = args[args.index('--ip') + 1]; env = bg.read_env(args[args.index('--env-file') + 1])
            cid = format(100 + len({v['Id'] for v in self.infos.values()}), '064x')
            self.add(name, cid, args[-1], env, {network: {'IPAddress': ip, 'GwPriority': 0}}, 'created')
            return cid
        if args[:2] == ('network', 'connect'):
            if self.connect_failure: raise RuntimeError('simulated data-network connection failure')
            assert args[2:4] == ('--gw-priority', '-1')
            network, cid = args[4:]; info = self.inspect(cid); address = '10.30.10.' + str(100 + len(self.networks[network]['Containers']))
            info['NetworkSettings']['Networks'][network] = {'IPAddress': address, 'GwPriority': -1}
            self.networks[network]['Containers'][cid] = {'Name': info['Name'][1:], 'IPv4Address': address + '/24'}
            return ''
        if args[0] == 'start': self.inspect(args[1])['State']['Status'] = 'running'; return ''
        raise AssertionError('Unexpected Docker operation: ' + repr(args))


class SplitSchemaTest(unittest.TestCase):
    def test_three_distinct_bridges_and_all_service_addresses_are_accepted(self):
        self.assertEqual(helper.validate_frontend_settings(policy()), policy())

    def test_overlap_duplicate_names_and_reserved_addresses_are_rejected(self):
        bad = []
        for section, field, value in [('blue', 'subnet', '10.10.10.0/24'), ('data', 'subnet', '10.20.10.0/24'),
                ('data', 'network', 'ieum_net_blue'), ('blue', 'network', 'ieum_net_green'),
                ('green', 'agentIp', '10.10.10.11'), ('blue', 'agentIp', '10.20.10.21'),
                ('blue', 'backendIp', '10.30.10.21'), ('blue', 'backendIp', '10.20.10.255')]:
            value_policy = policy(); value_policy[section][field] = value; bad.append(value_policy)
        p = policy(); p['data']['stores']['mongo'] = p['data']['stores']['redis']; bad.append(p)
        p = policy(); p['data']['stores']['mongo'] = 'ieum-green-backend'; bad.append(p)
        p = policy(); p['data']['extra'] = 'not accepted'; bad.append(p)
        p = policy(); p['version'] = True; bad.append(p)
        for value in bad:
            with self.subTest(policy=value), self.assertRaises(ValueError): helper.validate_frontend_settings(value)

    def test_nginx_uses_all_three_same_color_addresses_and_preserves_legacy_route(self):
        with patch.object(helper, 'frontend_policy', return_value=policy()):
            for color in ('blue', 'green'):
                result = helper.render(color)
                for key, port in [('ip', 80), ('backendIp', 8080), ('agentIp', 8000)]:
                    self.assertIn('server ' + policy()[color][key] + ':' + str(port) + ';', result)
            self.assertIn('server 10.10.10.11:8080;', helper.render('legacy'))


class SplitControllerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.value = policy(self.root / 'runtime'); self.fake = DockerFixture(self.value)
        ips = {c: {'frontend': self.value[c]['ip'], 'backend': self.value[c]['backendIp'], 'agent': self.value[c]['agentIp']} for c in ('blue', 'green')}
        for attribute, value in [('HOME', self.root), ('IPS', ips), ('FRONTEND_SETTINGS', self.value),
                ('FRONTEND_POLICY', self.root / 'policy.json'), ('ROUTE_CONFIG', self.root / 'route.conf')]:
            patcher = patch.object(bg, attribute, value); patcher.start(); self.addCleanup(patcher.stop)
        for attribute, kwargs in [('docker', {'side_effect': self.fake.docker}), ('inspect', {'side_effect': self.fake.inspect}),
                ('exists', {'side_effect': lambda name: name in self.fake.infos})]:
            patcher = patch.object(bg, attribute, **kwargs); patcher.start(); self.addCleanup(patcher.stop)
        bg.atomic(self.root / 'control/phase', 'legacy\n')

    def prepare_fixture(self):
        images = {}
        for index, app in enumerate(bg.APPS):
            images[app] = 'sha256:current-' + app
            attached = {'ieum_net_green': {'IPAddress': bg.IPS['green'][app], 'GwPriority': 0}}
            if app != 'frontend': attached['ieum_net_data'] = {'IPAddress': '10.30.10.' + str(index + 80), 'GwPriority': -1}
            env = environment(app)
            if app == 'backend': env['AGENT_SERVICE_URL'] = bg.base('green', 'agent')
            self.fake.add(bg.name('green', app), format(40 + index, '064x'), images[app], env, attached, 'running')
        bg.save({'active': 'green', 'previous': None, 'slots': {'green': {'status': 'active', 'images': images, 'release': bg.release_id(images)}}})
        bg.atomic(self.root / 'control/phase', 'ready\n'); bg.atomic(self.root / 'control/active', 'green\n')
        bg.atomic(self.root / 'control/pending-recovery-not-before', '2026-10-20T00:00:00Z\n')
        bg.atomic(self.root / 'control/backend-time-zone', 'UTC\n')
        bg.atomic(bg.ROUTE_CONFIG, '# Managed by ieum-bluegreen-nginx. active=green\n')

    def test_first_configuration_pins_all_networks_and_service_addresses_without_restarting_apps(self):
        route = bg.ROUTE_CONFIG; bg.atomic(route, '# Managed by ieum-bluegreen-nginx. active=legacy\n')
        source = self.root / 'candidate.json'; bg.atomic(source, helper.policy_text(self.value))
        original = helper.read_frontend_file
        def read(path, *, owner, mode):
            # Root ownership is simulated, other schema/private-file checks use real files.
            return original(path, owner=os.geteuid(), mode=mode)
        def run(*args):
            if args[:3] == ('python3', bg.NGINX_HELPER, 'validate-frontend'):
                return helper.policy_text(original(args[3], owner=os.geteuid(), mode=0o600))
            if args == ('python3', bg.NGINX_HELPER, 'read-frontend'):
                return helper.policy_text(helper.frontend_policy())
            if args == ('sudo', '-n', bg.NGINX_HELPER, 'configure-frontend'):
                helper.configure_frontend(); return ''
            self.fail('Unexpected external operation')
        with patch.object(helper, 'RUNTIME_ROOT', self.root / 'runtime'), \
             patch.object(helper, 'FRONTEND_POLICY', bg.FRONTEND_POLICY), patch.object(helper, 'DESTINATION', route), \
             patch.object(helper, 'FRONTEND_CANDIDATE', self.root / 'frontend-deployment-candidate.json'), \
             patch.object(helper, 'read_frontend_file', side_effect=read), patch.object(helper.os, 'chown'), \
             patch.object(helper.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=os.geteuid())), \
             patch.object(bg, 'INSTALLED_HOME', self.root), patch.object(bg, 'run', side_effect=run), \
             patch.object(bg.sys, 'argv', ['bluegreen', '--lock-timeout', '0', 'configure-frontend', '--settings-file', str(source)]):
            bg.main(); first = bg.FRONTEND_POLICY.read_bytes(); bg.main()
            self.assertEqual(bg.FRONTEND_POLICY.read_bytes(), first)
            changed = copy.deepcopy(self.value); changed['blue']['backendIp'] = '10.20.10.71'
            bg.atomic(source, helper.policy_text(changed))
            with self.assertRaisesRegex(RuntimeError, 'differ'): bg.main()
        self.assertEqual(json.loads(first), self.value)
        self.assertEqual((self.root / 'control/frontend-deployment-sha256').read_text().strip(),
            bg.hashlib.sha256(helper.policy_text(self.value).encode()).hexdigest())
        self.assertFalse((self.root / 'frontend-deployment-candidate.json').exists())
        self.assertFalse(any(args[0] in {'create', 'start'} for args in self.fake.calls))
        self.assertEqual(route.read_text(), '# Managed by ieum-bluegreen-nginx. active=legacy\n')
        for color in ('blue', 'green'):
            self.assertEqual(bg.IPS[color]['backend'], self.value[color]['backendIp'])
            self.assertEqual(bg.IPS[color]['agent'], self.value[color]['agentIp'])

    def test_network_preflight_checks_both_bridges_and_the_single_data_set(self):
        stores = bg.validate_frontend_network()
        self.assertEqual(set(stores), {'postgres', 'redis', 'mongo'})
        self.assertEqual(stores['postgres']['name'], 'ieum-postgres')
        self.assertEqual({args[2] for args in self.fake.calls if args[:2] == ('network', 'inspect')},
                         {'ieum_net_blue', 'ieum_net_green', 'ieum_net_data'})
        self.assertFalse(any(args[0] in {'create', 'start'} for args in self.fake.calls))

    def test_old_api_driver_subnet_and_gateway_conflicts_fail_before_mutation(self):
        original = copy.deepcopy(self.fake.networks)
        for api in ('1.47 1.48', '1.48 1.47', 'unknown', '1.48'):
            self.fake.api = api
            with self.assertRaises(RuntimeError): bg.validate_frontend_network()
        self.fake.api = '1.48 1.48'
        for key, value in [('Driver', 'host'), ('Name', 'other'), ('Internal', True)]:
            self.fake.networks = copy.deepcopy(original); self.fake.networks['ieum_net_blue'][key] = value
            with self.assertRaises(RuntimeError): bg.validate_frontend_network()
        self.fake.networks = copy.deepcopy(original)
        self.fake.networks['ieum_net_blue']['IPAM']['Config'][0]['Gateway'] = self.value['blue']['backendIp']
        with self.assertRaises(RuntimeError): bg.validate_frontend_network()

    def test_infrastructure_ip_collision_is_rejected_but_target_slot_can_be_replaced(self):
        network = self.fake.networks['ieum_net_blue']; cid = 'f' * 64
        network['Containers'][cid] = {'Name': 'unrelated', 'IPv4Address': self.value['blue']['backendIp'] + '/24'}
        with self.assertRaises(RuntimeError): bg.validate_frontend_network()
        network['Containers'][cid]['Name'] = bg.name('blue', 'backend')
        bg.validate_frontend_network(colors=('blue',), replaceable={bg.name('blue', app) for app in bg.APPS})

    def test_stores_must_be_running_and_frontend_is_never_on_data_bridge(self):
        self.fake.inspect('ieum-postgres')['State']['Status'] = 'exited'
        with self.assertRaises(RuntimeError): bg.validate_frontend_network()
        self.fake.inspect('ieum-postgres')['State']['Status'] = 'running'
        self.fake.networks['ieum_net_data']['Containers']['f' * 64] = {'Name': bg.name('blue', 'frontend'), 'IPv4Address': '10.30.10.50/24'}
        with self.assertRaises(RuntimeError): bg.validate_frontend_network()

    def test_legacy_store_multihoming_is_allowed_only_during_bootstrap(self):
        info = self.fake.inspect('ieum-postgres'); info['NetworkSettings']['Networks']['ieum_net_green'] = {'IPAddress': '10.10.10.13'}
        bg.validate_frontend_network()
        with self.assertRaises(RuntimeError): bg.validate_split_networks(self.value, require_data_only=True)
        bg.atomic(self.root / 'control/phase', 'ready\n')
        with self.assertRaises(RuntimeError): bg.validate_frontend_network()

    def test_shared_data_addresses_or_names_are_required_and_errors_never_echo_credentials(self):
        stores = bg.validate_frontend_network(); env = {app: environment(app) for app in bg.APPS}
        bg.validate_data_environment(env, stores)
        variants = [('backend', 'DB_URL', 'jdbc:postgresql://10.10.10.13/private-secret'),
            ('backend', 'REDIS_HOST', '10.10.10.12'), ('backend', 'MONGO_URI', 'mongodb://user:private-secret@other/db'),
            ('agent', 'MONGODB_URL', 'mongodb://other/db'), ('agent', 'MONGODB_URL', 'mongodb+srv://ieum-mongo/db')]
        for app, key, value in variants:
            changed = copy.deepcopy(env); changed[app][key] = value
            with self.assertRaises(RuntimeError) as caught: bg.validate_data_environment(changed, stores)
            self.assertNotIn('private-secret', str(caught.exception))
        env['backend']['MONGO_URI'] = 'mongodb://user:pass,word@ieum-mongo/ieum'
        bg.validate_data_environment(env, stores)

    def test_prepare_creates_three_apps_in_color_bridge_and_only_workers_join_data(self):
        self.prepare_fixture()
        with patch.object(bg, 'check', return_value={'draining': True, 'schedulerStandby': True}), \
             patch.object(bg, 'remove_inactive', side_effect=bg.invalidate_slot):
            bg.prepare('blue', {'frontend': 'sha256:new-frontend'})
        creates = [args for args in self.fake.calls if args[0] == 'create']
        self.assertEqual(len(creates), 3)
        self.assertTrue(all(args[args.index('--network') + 1] == 'ieum_net_blue' for args in creates))
        connects = [args for args in self.fake.calls if args[:2] == ('network', 'connect')]
        self.assertEqual(len(connects), 2)
        for app in bg.APPS:
            info = self.fake.inspect(bg.name('blue', app)); attached = info['NetworkSettings']['Networks']
            self.assertEqual(set(attached), {'ieum_net_blue'} if app == 'frontend' else {'ieum_net_blue', 'ieum_net_data'})
            env = dict(item.split('=', 1) for item in info['Config']['Env'])
            if app == 'agent': self.assertEqual(env['BACKEND_BASE_URL'], bg.base('blue', 'backend'))
            if app == 'backend': self.assertEqual(env['AGENT_SERVICE_URL'], bg.base('blue', 'agent'))
        bg.validate_slot_networks('blue')
        self.assertEqual(bg.state()['active'], 'green'); self.assertEqual(bg.state()['slots']['blue']['status'], 'candidate')

    def test_failed_data_connection_cleans_created_ids_without_switching_active(self):
        self.prepare_fixture(); self.fake.connect_failure = True
        with patch.object(bg, 'remove_inactive', side_effect=bg.invalidate_slot), \
             patch.object(bg, 'cleanup_candidate_containers', return_value={'complete': True, 'containers': []}) as cleanup, \
             patch.object(bg, 'route') as route:
            with self.assertRaisesRegex(RuntimeError, 'data-network'): bg.prepare('blue', {'frontend': 'sha256:new'})
        cleanup.assert_called_once(); route.assert_not_called()
        self.assertEqual(bg.state()['active'], 'green'); self.assertEqual(bg.state()['slots']['blue']['status'], 'failed')
        started = [args[1] for args in self.fake.calls if args[0] == 'start']
        self.assertEqual(len(started), 1)  # Only frontend started before the agent connect failed.

    def test_network_drift_and_wrong_agent_backend_are_rejected_on_recheck(self):
        self.prepare_fixture()
        bg.validate_slot_networks('green')
        info = self.fake.inspect(bg.name('green', 'agent'))
        info['NetworkSettings']['Networks']['ieum_net_data']['GwPriority'] = 0
        with self.assertRaises(RuntimeError): bg.validate_slot_networks('green')
        info['NetworkSettings']['Networks']['ieum_net_data']['GwPriority'] = -1
        info['Config']['Env'] = ['MONGODB_URL=mongodb://ieum-mongo/db', 'BACKEND_BASE_URL=http://wrong.invalid']
        with self.assertRaisesRegex(RuntimeError, 'Agent backend'): bg.validate_slot_networks('green')

    def test_data_validation_and_interface_drift_block_before_slot_replacement(self):
        self.prepare_fixture()
        info = self.fake.inspect(bg.name('green', 'backend'))
        info['Config']['Env'] = ['DB_URL=jdbc:postgresql://wrong.invalid/private-secret',
                                'REDIS_HOST=ieum-redis', 'MONGO_URI=mongodb://ieum-mongo/db']
        with patch.object(bg, 'remove_inactive') as remove:
            with self.assertRaisesRegex(RuntimeError, 'datastore'): bg.prepare('blue', {'frontend': 'sha256:new'})
        remove.assert_not_called()
        self.assertFalse(any(args[0] == 'create' for args in self.fake.calls))
        self.assertEqual(bg.state()['active'], 'green')

    def test_frontend_or_cross_color_attachments_are_rejected(self):
        self.prepare_fixture()
        front = self.fake.inspect(bg.name('green', 'frontend'))
        front['NetworkSettings']['Networks']['ieum_net_data'] = {'IPAddress': '10.30.10.200', 'GwPriority': -1}
        with self.assertRaises(RuntimeError): bg.validate_slot_networks('green')
        del front['NetworkSettings']['Networks']['ieum_net_data']
        agent = self.fake.inspect(bg.name('green', 'agent'))
        agent['NetworkSettings']['Networks']['ieum_net_blue'] = {'IPAddress': '10.20.10.200', 'GwPriority': 0}
        with self.assertRaises(RuntimeError): bg.validate_slot_networks('green')

    def test_finish_bootstrap_waits_for_store_network_migration_before_enabling_distributed_mode(self):
        self.prepare_fixture(); bg.atomic(self.root / 'control/phase', 'legacy\n')
        database = self.fake.inspect('ieum-postgres'); original_id = database['Id']
        database['NetworkSettings']['Networks']['ieum_net_green'] = {'IPAddress': '10.10.10.13'}
        for app in bg.APPS:
            self.fake.add(bg.LEGACY[app], 'e' * 63 + str(bg.APPS.index(app)), 'sha256:legacy-' + app,
                {}, {'ieum_net_green': {'IPAddress': {'frontend': '10.10.10.10', 'backend': '10.10.10.11', 'agent': '10.10.10.14'}[app]}}, 'running')
        original_docker = self.fake.docker
        def docker(*args):
            if args[0] == 'exec':
                self.fake.calls.append(args)
                return '0' if 'XLEN' in args else ''
            if args[0] == 'stop':
                self.fake.calls.append(args); self.fake.inspect(args[-1])['State']['Status'] = 'exited'; return ''
            return original_docker(*args)
        with patch.object(bg, 'docker', side_effect=docker), patch.object(bg, 'pg', return_value='0'), \
             patch.object(bg, 'control', return_value={'schedulerStandby': False}):
            with self.assertRaisesRegex(RuntimeError, 'only the data network'): bg.finish_bootstrap()
            self.assertEqual((self.root / 'control/phase').read_text(), 'legacy\n')
            self.assertFalse(any('SET' in args for args in self.fake.calls))
            del database['NetworkSettings']['Networks']['ieum_net_green']  # Simulate the reviewed operator detach.
            bg.finish_bootstrap()
        self.assertEqual((self.root / 'control/phase').read_text(), 'ready\n')
        self.assertEqual(database['Id'], original_id)
        self.assertTrue(any(args[:4] == ('exec', 'ieum-redis', 'redis-cli', 'SET') for args in self.fake.calls))
        self.assertTrue(all(self.fake.inspect(bg.LEGACY[app])['State']['Status'] == 'exited' for app in bg.APPS))

    def test_postgres_operator_queries_use_the_same_configured_container(self):
        self.value['data']['stores']['postgres'] = 'custom-postgres'
        with patch.object(bg, 'run', return_value='0') as run:
            self.assertEqual(bg.pg('select 1'), '0')
        self.assertEqual(run.call_args.args[:3], ('docker', 'exec', 'custom-postgres'))

    def test_switch_and_rollback_keep_network_topology_pinned(self):
        self.prepare_fixture()
        with patch.object(bg, 'check', return_value={'draining': True, 'schedulerStandby': True}), \
             patch.object(bg, 'remove_inactive', side_effect=bg.invalidate_slot): bg.prepare('blue', {'frontend': 'sha256:new'})
        images_before = {c: bg.slot_images(c) for c in ('blue', 'green')}
        with patch.object(bg, 'check', side_effect=lambda color: (bg.validate_slot_networks(color) or {})), \
             patch.object(bg, 'control', return_value={'activeJobs': 0, 'queuedJobs': 0, 'activeRequests': 0, 'sseSubscribers': 0, 'websocketUsers': 0}), \
             patch.object(bg, 'route', side_effect=lambda color: bg.atomic(bg.ROUTE_CONFIG, '# Managed by ieum-bluegreen-nginx. active=' + color + '\n')), \
             patch.object(bg, 'verify_ingress'), patch.object(bg, 'archive_frontend'):
            bg.switch('blue')
            bg.rollback(bg.state()['slots']['green']['release'])
        self.assertEqual(bg.state()['active'], 'green')
        self.assertEqual(images_before, {c: bg.slot_images(c) for c in ('blue', 'green')})
        bg.validate_slot_networks('blue'); bg.validate_slot_networks('green')


if __name__ == '__main__': unittest.main()
