"""MAIN data isolation and fresh provisioning regressions; synthetic values only."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location('main_infra_under_test', Path(__file__).with_name('infra_env.py'))
infra = importlib.util.module_from_spec(spec)
spec.loader.exec_module(infra)
SECRET = 'synthetic-main-private-value'


def limits():
    values = {'MAIN_INFRA_LOG_MAX_SIZE': '10m', 'MAIN_INFRA_LOG_MAX_FILES': '3',
              'MAIN_NOTION_MCP_VERSION': '1.2.3'}  # Synthetic metadata; not an operating version recommendation.
    for service in infra.shared.SCHEMAS:
        prefix = service.replace('-', '_').upper()
        values['MAIN_' + prefix + '_CPUS'] = '0.5'
        values['MAIN_' + prefix + '_MEMORY'] = '256m'
    return values


def secrets():
    values = {'DATA_DOCKER_NETWORK': infra.NETWORK, 'DATA_DOCKER_NETWORK_SUBNET': '10.60.10.0/24',
              'HOST_BIND_IP': '127.0.0.1',
              'POSTGRES_ENV_FILE': str(infra.ENV_HOME / 'postgres.main.env'),
              'POSTGRES_CONTAINER_NAME': 'ieum-main-postgres', 'POSTGRES_IP': '10.60.10.13',
              'POSTGRES_PORT': '5432', 'POSTGRES_HOST_PORT': '15432', 'POSTGRES_DB': 'ieum_main',
              'POSTGRES_USER': 'synthetic_main', 'POSTGRES_PASSWORD': SECRET,
              'MONGO_ENV_FILE': str(infra.ENV_HOME / 'mongo.main.env'),
              'MONGO_CONTAINER_NAME': 'ieum-main-mongo', 'MONGO_IP': '10.60.10.15',
              'MONGO_PORT': '27017', 'MONGO_HOST_PORT': '27018', 'MONGO_INITDB_DATABASE': 'ieum_main',
              'MONGO_INITDB_ROOT_USERNAME': 'synthetic_admin', 'MONGO_INITDB_ROOT_PASSWORD': SECRET,
              'REDIS_ENV_FILE': str(infra.ENV_HOME / 'redis.main.env'),
              'REDIS_CONTAINER_NAME': 'ieum-main-redis', 'REDIS_IP': '10.60.10.12',
              'PHOENIX_ENV_FILE': str(infra.ENV_HOME / 'phoenix.main.env'),
              'PHOENIX_CONTAINER_NAME': 'ieum-main-phoenix', 'PHOENIX_UI_PORT': '6007',
              'NOTION_MCP_ENV_FILE': str(infra.ENV_HOME / 'notion-mcp.main.env'),
              'NOTION_MCP_CONTAINER_NAME': 'ieum-main-notion-mcp', 'NOTION_MCP_PORT': '3000',
              'NOTION_MCP_AUTH_TOKEN': SECRET}
    return {infra.PREFIX + k: v for k, v in values.items()}


def settings(service='postgres', changes=None, resources=None):
    values = secrets()
    values.update(changes or {})
    return infra.build_settings(json.dumps(values), service,
                                json.dumps(limits() if resources is None else resources))


def live(selected):
    service = selected['service']
    prefix = service.replace('-', '_').upper()
    labels = {'com.docker.compose.project': selected['project'],
              infra.ENVIRONMENT_LABEL: 'main', infra.SERVICE_LABEL: service}
    network = {'Name': infra.NETWORK, 'Driver': 'bridge', 'Internal': False,
               'Labels': {infra.ENVIRONMENT_LABEL: 'main'},
               'IPAM': {'Config': [{'Subnet': '10.60.10.0/24', 'Gateway': '10.60.10.1'}]}}
    container = {'Name': '/' + selected['name'], 'State': {'Running': True, 'Health': {'Status': 'healthy'}},
                 'Config': {'Labels': labels},
                 'NetworkSettings': {'Networks': {infra.NETWORK: {
                     'IPAddress': selected['values'].get(prefix + '_IP', '10.60.10.50')}}},
                 'HostConfig': {'PortBindings': {'internal/tcp': [
                     {'HostIp': '127.0.0.1', 'HostPort': infra.expected_host_port(selected)}]}}, 'Mounts': []}
    volume = {'Labels': labels}
    if service in infra.shared.VOLUMES:
        logical, destination = infra.shared.VOLUMES[service]
        container['Mounts'] = [{'Type': 'volume', 'Name': selected['project'] + '_' + logical,
                                'Destination': destination}]
    return {'network': network, 'container': container, 'volume': volume}


class SettingsTests(unittest.TestCase):
    def test_all_services_select_main_only_with_unique_projects_and_resources(self):
        for service in infra.shared.SCHEMAS:
            values = secrets()
            values['DEV_COM_POSTGRES_PASSWORD'] = 'not-selected'
            actual = infra.build_settings(json.dumps(values), service, json.dumps(limits()))
            self.assertEqual(actual['name'], 'ieum-main-' + service)
            self.assertEqual(actual['project'], 'ieum-main-' + service)
            self.assertEqual(actual['values']['GREEN_NETWORK'], infra.NETWORK)
            self.assertEqual(actual['values']['SERVICE_CPUS'], '0.5')
            self.assertNotIn(service.replace('-', '_').upper() + '_ENV_FILE', actual['values'])

    def test_main_cannot_fall_back_to_dev_aws_or_unprefixed_secrets(self):
        for prefix in ('DEV_COM_', 'AWS_', '', 'MAIN_'):
            payload = {prefix + k.removeprefix(infra.PREFIX): v for k, v in secrets().items()}
            with self.subTest(prefix=prefix), self.assertRaises(infra.ConfigurationError):
                infra.build_settings(json.dumps(payload), 'postgres', json.dumps(limits()))

    def test_required_values_and_literal_secrets(self):
        for key in ('POSTGRES_DB', 'POSTGRES_PASSWORD', 'DATA_DOCKER_NETWORK_SUBNET'):
            for value in ('', '   ', None, 7, SECRET + '\nINJECTED=yes', '<password>'):
                with self.subTest(key=key, value=value), self.assertRaises(infra.ConfigurationError):
                    settings(changes={infra.PREFIX + key: value})
        literal = '  서울 $VAR ${OTHER} $(touch ignored) `echo x` " \' # = \\  '
        self.assertEqual(settings(changes={infra.PREFIX + 'POSTGRES_PASSWORD': literal})['values']['POSTGRES_PASSWORD'], literal)

    def test_main_rejects_dev_container_network_path_and_default_host_ports(self):
        cases = {'POSTGRES_CONTAINER_NAME': ['ieum-postgres', 'ieum-blue-backend'],
                 'DATA_DOCKER_NETWORK': ['ieum_net_data', 'ieum_net_green', 'bridge'],
                 'POSTGRES_ENV_FILE': ['/home/kwung/deploy/ieum-infra/postgres.main.env', '/tmp/postgres.env'],
                 'POSTGRES_HOST_PORT': ['5432', '27017', '6379', '6006', '3000'],
                 'HOST_BIND_IP': ['0.0.0.0', '192.168.0.11', '::1']}
        for key, values in cases.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(infra.ConfigurationError):
                    settings(changes={infra.PREFIX + key: value})

    def test_main_defaults_are_distinct_loopback_ports(self):
        self.assertEqual(settings('redis')['values']['REDIS_HOST_PORT'], '16379')
        self.assertEqual(settings('notion-mcp')['values']['NOTION_MCP_HOST_PORT'], '13000')
        data = secrets()
        data.pop(infra.PREFIX + 'HOST_BIND_IP')
        actual = infra.build_settings(json.dumps(data), 'phoenix', json.dumps(limits()))
        self.assertEqual(actual['values']['HOST_BIND_IP'], '127.0.0.1')

    def test_resource_limits_have_no_implicit_capacity_defaults(self):
        for key in ('MAIN_POSTGRES_CPUS', 'MAIN_POSTGRES_MEMORY', 'MAIN_INFRA_LOG_MAX_SIZE', 'MAIN_INFRA_LOG_MAX_FILES'):
            values = limits()
            values.pop(key)
            with self.subTest(key=key), self.assertRaises(infra.ConfigurationError):
                settings(resources=values)
        cases = {'MAIN_POSTGRES_CPUS': ['0', '-1', 'nan', '1; echo bad', ' 1'],
                 'MAIN_POSTGRES_MEMORY': ['0m', '-1g', 'all', '256m\nBAD=x'],
                 'MAIN_INFRA_LOG_MAX_SIZE': ['0m', 'unlimited'],
                 'MAIN_INFRA_LOG_MAX_FILES': ['0', '-1', 'all']}
        for key, values in cases.items():
            for value in values:
                resource = {**limits(), key: value}
                with self.subTest(key=key, value=value), self.assertRaises(infra.ConfigurationError):
                    settings(resources=resource)

    def test_main_mcp_requires_an_exact_operator_selected_version(self):
        for value in (None, '', True, 123, 'latest', '^1.2.3', '1.2', '1.2.3-beta',
                      '1.2.3+build', '01.2.3', '1.2.3\nNEXT=x'):
            resource = limits()
            if value is None:
                resource.pop('MAIN_NOTION_MCP_VERSION')
            else:
                resource['MAIN_NOTION_MCP_VERSION'] = value
            with self.subTest(value=value), self.assertRaises(infra.ConfigurationError):
                settings('notion-mcp', resources=resource)
        self.assertEqual(settings('notion-mcp')['values']['NOTION_MCP_VERSION'], '1.2.3')
        self.assertNotIn('NOTION_MCP_VERSION', settings('postgres')['values'])
        source = (infra.ROOT / 'compose/notion-mcp.main.Dockerfile').read_text()
        self.assertNotIn('@latest', source)
        self.assertIn('ARG NOTION_MCP_VERSION', source)
        self.assertIn('"--no-install"', source)

    def test_malformed_and_duplicate_json_are_rejected(self):
        for payload in (None, '', '[]', '{', '{"x":NaN}', '[' * 1500,
                        json.dumps(secrets())[:-1] + ',"MAIN_COM_POSTGRES_PASSWORD":"duplicate"}'):
            with self.assertRaises(infra.ConfigurationError):
                infra.build_settings(payload, 'postgres', json.dumps(limits()))

    def test_mutation_is_disabled_by_default_but_offline_check_is_available(self):
        for operation in ('deploy', 'bootstrap'):
            out, err = io.StringIO(), io.StringIO()
            with patch.object(infra, 'deploy') as command, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = infra.run_operation(json.dumps(secrets()), 'postgres', limits_payload=json.dumps(limits()),
                                           enabled=None, operation=operation)
            self.assertEqual(code, 1)
            command.assert_not_called()
            self.assertNotIn(SECRET, out.getvalue() + err.getvalue())
        with patch.object(infra, 'deploy') as command, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(infra.run_operation(json.dumps(secrets()), 'postgres', limits_payload=json.dumps(limits()),
                                                enabled=None, operation='check'), 0)
        command.assert_not_called()

    def test_cli_profile_dispatch_unsets_secret_and_variable_transports(self):
        def observe(payload, service, **kwargs):
            self.assertNotIn(infra.shared.TRANSPORT, os.environ)
            self.assertNotIn(infra.RESOURCE_TRANSPORT, os.environ)
            self.assertEqual(kwargs['operation'], 'bootstrap')
            return 0
        with patch.dict(os.environ, {infra.shared.TRANSPORT: json.dumps(secrets()),
                                    infra.RESOURCE_TRANSPORT: json.dumps(limits()), 'MAIN_DEPLOY_ENABLED': 'true'}), \
                patch.object(infra.shared, 'main_profile_module', return_value=infra), \
                patch.object(infra, 'run_operation', side_effect=observe):
            self.assertEqual(infra.shared.main(['--profile', 'main', '--service', 'postgres', '--bootstrap']), 0)

    def test_public_profile_dispatch_and_separate_bootstrap_deploy_gates(self):
        actual = infra.shared.build_settings(json.dumps(secrets()), 'postgres', profile='main',
                                            limits_payload=json.dumps(limits()))
        self.assertEqual(actual['project'], 'ieum-main-postgres')
        for operation, deploy_enabled, bootstrap_enabled, expected in (
                ('bootstrap', 'true', None, 1), ('deploy', None, 'true', 1),
                ('bootstrap', None, 'true', 0), ('deploy', 'true', None, 0)):
            with self.subTest(operation=operation, deploy_enabled=deploy_enabled), \
                    patch.object(infra, 'deploy') as mutate, contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                code = infra.run_operation(json.dumps(secrets()), 'postgres', limits_payload=json.dumps(limits()),
                    enabled=deploy_enabled, bootstrap_enabled=bootstrap_enabled, operation=operation)
            self.assertEqual(code, expected)
            self.assertEqual(mutate.call_count, 0 if expected else 1)


class OwnershipTests(unittest.TestCase):
    def test_main_live_services_require_environment_project_network_ports_and_volumes(self):
        for service in infra.shared.SCHEMAS:
            selected = settings(service)
            objects = live(selected)
            with patch.object(infra, 'inspect', side_effect=lambda kind, name: objects[kind]):
                infra.validate_live(selected)

    def test_unowned_and_dev_resources_are_rejected(self):
        mutations = [lambda o: o['network']['Labels'].clear(),
                     lambda o: o['network'].update(Name='ieum_net_data'),
                     lambda o: o['container']['Config']['Labels'].update({infra.ENVIRONMENT_LABEL: 'dev'}),
                     lambda o: o['container']['Config']['Labels'].update({'com.docker.compose.project': 'ieum-green-postgres'}),
                     lambda o: o['container']['NetworkSettings']['Networks'].update(ieum_net_green={}),
                     lambda o: o['container']['HostConfig']['PortBindings']['internal/tcp'][0].update(HostIp='0.0.0.0'),
                     lambda o: o['container']['Mounts'][0].update(Name='ieum-green-postgres_ieum_postgres_data'),
                     lambda o: o['volume']['Labels'].update({infra.ENVIRONMENT_LABEL: 'dev'})]
        for mutate in mutations:
            objects = live(settings())
            mutate(objects)
            with self.subTest(mutate=mutate), patch.object(infra, 'inspect', side_effect=lambda kind, name: objects[kind]), \
                    self.assertRaises(infra.ConfigurationError):
                infra.validate_live(settings())


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.root.chmod(0o700)
        self.selected = settings()
        self.selected['path'] = self.root / 'postgres.main.env'
        self.objects = live(self.selected)

    def test_fresh_bootstrap_only_creates_main_service_and_preserves_dev(self):
        commands = []
        def run(args, **kwargs):
            commands.append(args)
            if args[:3] == ['docker', 'ps', '--all'] and '--filter' not in args:
                return 'ieum-postgres\nieum-green-phoenix\n'
            if args[:3] == ['docker', 'volume', 'ls']:
                return 'ieum-green-postgres_ieum_postgres_data\n'
            return ''
        with patch.object(infra, 'ENV_HOME', self.root), patch.object(infra.shared, 'deployment_lock', contextlib.nullcontext), \
                patch.object(infra, 'run', side_effect=run), \
                patch.object(infra, 'inspect', side_effect=lambda kind, name: self.objects[kind]), \
                patch.object(infra.socket, 'socket'):
            infra.deploy(self.selected, bootstrap=True)
        updates = [args for args in commands if 'up' in args]
        self.assertEqual(len(updates), 1)
        self.assertIn('ieum-main-postgres', updates[0])
        self.assertIn('missing', updates[0])
        self.assertTrue(self.selected['path'].exists())
        for args in commands:
            self.assertFalse(set(args) & {'down', 'rm', 'prune', '--remove-orphans', '-v'})
            self.assertNotEqual(args[:3], ['docker', 'network', 'create'])

    def test_bootstrap_refuses_existing_container_volume_project_or_env(self):
        for kind in ('container', 'volume', 'project', 'env'):
            if kind == 'env':
                self.selected['path'].write_text('existing private config\n')
                self.selected['path'].chmod(0o600)
            def run(args, **kwargs):
                if kind == 'container' and args[:3] == ['docker', 'ps', '--all'] and '--filter' not in args:
                    return self.selected['name']
                if kind == 'project' and '--filter' in args:
                    return 'unowned-orphan'
                if kind == 'volume' and args[:3] == ['docker', 'volume', 'ls']:
                    return self.selected['project'] + '_ieum_postgres_data'
                return ''
            with self.subTest(kind=kind), patch.object(infra, 'ENV_HOME', self.root), \
                    patch.object(infra, 'validate_network'), patch.object(infra, 'run', side_effect=run), \
                    self.assertRaises(infra.ConfigurationError):
                infra.bootstrap_preflight(self.selected)
        self.assertEqual(self.selected['path'].read_text(), 'existing private config\n')

    def test_failed_bootstrap_does_not_remove_created_resources(self):
        commands = []
        def fail(args, **kwargs):
            commands.append(args)
            if 'up' in args:
                raise infra.ConfigurationError('synthetic_failure')
            return ''
        with patch.object(infra, 'ENV_HOME', self.root), patch.object(infra.shared, 'deployment_lock', contextlib.nullcontext), \
                patch.object(infra, 'bootstrap_preflight'), patch.object(infra, 'run', side_effect=fail):
            with self.assertRaises(infra.ConfigurationError):
                infra.deploy(self.selected, bootstrap=True)
        self.assertFalse(self.selected['path'].exists())
        self.assertTrue(all(not set(args) & {'rm', 'down', 'prune', '-v'} for args in commands))

    def test_update_requires_existing_ownership_before_writing_configuration(self):
        with patch.object(infra.shared, 'deployment_lock', contextlib.nullcontext), \
                patch.object(infra, 'validate_live', side_effect=infra.ConfigurationError('unowned')), \
                patch.object(infra, 'run') as command:
            with self.assertRaises(infra.ConfigurationError):
                infra.deploy(self.selected)
        command.assert_not_called()
        self.assertFalse(self.selected['path'].exists())


class WorkflowTests(unittest.TestCase):
    def test_all_main_services_include_guarded_explicit_profile_and_bootstrap(self):
        for service in infra.shared.SCHEMAS:
            source = (infra.ROOT / '.github/workflows' / (service + '-main-deploy.yml')).read_text()
            self.assertIn("vars.MAIN_DEPLOY_ENABLED == 'true'", source)
            self.assertIn('--profile main --service ' + service, source)
            self.assertIn('options: [check, deploy, bootstrap]', source)
            self.assertIn('infra_main_shared_services', source)
            self.assertIn('${{ toJSON(secrets) }}', source)
            self.assertIn('${{ toJSON(vars) }}', source)
            self.assertNotIn('AWS_', source)
            self.assertNotIn('docker rm', source)
            self.assertNotIn('down -v', source)
            self.assertNotIn('  push:', source)
            self.assertIn("github.ref == 'refs/heads/main'", source)
            self.assertIn('environment: main', source)
            self.assertIn("vars.MAIN_BOOTSTRAP_ENABLED == 'true'", source)
            self.assertIn('ref: ${{ github.sha }}', source)
            self.assertIn('persist-credentials: false', source)


class ComposeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which('docker') is None:
            raise unittest.SkipTest('Docker CLI unavailable; no daemon required')
        result = subprocess.run(['docker', 'compose', 'version'], capture_output=True, timeout=10)
        if result.returncode:
            raise unittest.SkipTest('Docker Compose CLI unavailable')

    def test_main_models_are_unique_limited_loopback_and_keep_literal_passwords(self):
        for service in infra.shared.SCHEMAS:
            selected = settings(service)
            if service == 'postgres':
                selected['values']['POSTGRES_PASSWORD'] = '  $VAR ${OTHER} \' " # = \\ 서울  '
            with tempfile.TemporaryDirectory() as directory:
                env = Path(directory) / 'private.env'
                env.write_bytes(infra.shared.compose_environment(selected['values']))
                result = subprocess.run(['docker', 'compose', '--project-name', selected['project'], '--env-file', str(env),
                    '-f', str(infra.ROOT / 'compose' / (service + '.main.compose.yml')), 'config', '--format', 'json'],
                    env=infra.shared.command_environment(), capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, 'Offline MAIN Compose model rejected')
                model = json.loads(result.stdout)
            actual = model['services'][service]
            self.assertEqual(actual['container_name'], 'ieum-main-' + service)
            self.assertEqual(actual['labels'][infra.ENVIRONMENT_LABEL], 'main')
            self.assertEqual(actual['cpus'], 0.5)
            self.assertGreater(int(actual['mem_limit']), 0)
            self.assertEqual(actual['logging']['options']['max-file'], '3')
            self.assertTrue(all(p['host_ip'] == '127.0.0.1' and int(p['published']) not in infra.DEV_HOST_PORTS for p in actual['ports']))
            if service in infra.shared.VOLUMES:
                logical, _ = infra.shared.VOLUMES[service]
                self.assertEqual(model['volumes'][logical]['name'], selected['project'] + '_' + logical)
                self.assertEqual(model['volumes'][logical]['labels'][infra.ENVIRONMENT_LABEL], 'main')
            if service == 'postgres':
                self.assertEqual(actual['environment']['POSTGRES_PASSWORD'].replace('$$', '$'), selected['values']['POSTGRES_PASSWORD'])
            if service == 'notion-mcp':
                self.assertEqual(actual['image'], 'ieum-notion-mcp:main')
                self.assertIn('healthcheck', actual)
                self.assertEqual(actual['build']['args']['NOTION_MCP_VERSION'], '1.2.3')
                self.assertEqual(actual['build']['dockerfile'], 'notion-mcp.main.Dockerfile')


if __name__ == '__main__':
    unittest.main()
