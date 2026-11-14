"""Synthetic configuration, fake deployment and offline Compose regressions."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location('infra_env_under_test', Path(__file__).with_name('infra_env.py'))
infra = importlib.util.module_from_spec(spec)
spec.loader.exec_module(infra)
SECRET = 'synthetic-private-value'


def secrets():
    values = {
        'DATA_DOCKER_NETWORK': 'ieum_net_data', 'DATA_DOCKER_NETWORK_SUBNET': '10.30.10.0/24',
        'HOST_BIND_IP': '127.0.0.1',
        'POSTGRES_ENV_FILE': str(infra.ENV_HOME / 'postgres.green.env'),
        'POSTGRES_CONTAINER_NAME': 'ieum-postgres', 'POSTGRES_IP': '10.30.10.13',
        'POSTGRES_PORT': '5432', 'POSTGRES_HOST_PORT': '5432', 'POSTGRES_DB': 'ieum',
        'POSTGRES_USER': 'ieum', 'POSTGRES_PASSWORD': SECRET,
        'MONGO_ENV_FILE': str(infra.ENV_HOME / 'mongo.green.env'),
        'MONGO_CONTAINER_NAME': 'ieum-mongo', 'MONGO_IP': '10.30.10.15',
        'MONGO_PORT': '27017', 'MONGO_HOST_PORT': '27017', 'MONGO_INITDB_DATABASE': 'ieum',
        'MONGO_INITDB_ROOT_USERNAME': 'synthetic_admin', 'MONGO_INITDB_ROOT_PASSWORD': SECRET,
        'REDIS_ENV_FILE': str(infra.ENV_HOME / 'redis.green.env'),
        'REDIS_CONTAINER_NAME': 'ieum-redis', 'REDIS_IP': '10.30.10.12',
        'PHOENIX_ENV_FILE': str(infra.ENV_HOME / 'phoenix.green.env'),
        'PHOENIX_CONTAINER_NAME': 'ieum-green-phoenix', 'PHOENIX_UI_PORT': '6006',
        'NOTION_MCP_ENV_FILE': str(infra.ENV_HOME / 'notion-mcp.green.env'),
        'NOTION_MCP_CONTAINER_NAME': 'ieum-notion-mcp-green', 'NOTION_MCP_PORT': '3000',
        'NOTION_MCP_AUTH_TOKEN': SECRET,
    }
    return {'DEV_COM_' + name: value for name, value in values.items()}


def settings(service='postgres', changes=None):
    values = secrets()
    values.update(changes or {})
    return infra.build_settings(json.dumps(values), service)


def live(service='postgres'):
    selected = settings(service)
    prefix = service.replace('-', '_').upper()
    network = {'Driver': 'bridge', 'Internal': False,
               'IPAM': {'Config': [{'Subnet': '10.30.10.0/24', 'Gateway': '10.30.10.1'}]}}
    container = {'Name': '/' + selected['name'], 'State': {'Running': True, 'Health': {'Status': 'healthy'}},
                 'Config': {'Labels': {'com.docker.compose.project': selected['project']}},
                 'NetworkSettings': {'Networks': {'ieum_net_data': {
                     'IPAddress': selected['values'].get(prefix + '_IP', '10.30.10.50')}}}, 'Mounts': []}
    if service in infra.VOLUMES:
        logical, destination = infra.VOLUMES[service]
        container['Mounts'] = [{'Type': 'volume', 'Name': selected['project'] + '_' + logical,
                                'Destination': destination}]
    return network, container


class SettingsTests(unittest.TestCase):
    def test_all_five_services_use_common_network_and_only_their_own_settings(self):
        self.assertEqual(len(infra.REQUIRED_SECRETS), 29)
        self.assertEqual(len(infra.OPTIONAL_SECRETS), 2)
        for service in infra.SCHEMAS:
            actual = settings(service)
            self.assertEqual(actual['values']['GREEN_NETWORK'], 'ieum_net_data')
            self.assertEqual(actual['values']['GREEN_NETWORK_SUBNET'], '10.30.10.0/24')
            self.assertNotIn('ENV_FILE', actual['values'])
            self.assertNotIn(service.replace('-', '_').upper() + '_ENV_FILE', actual['values'])
            if service != 'postgres':
                self.assertNotIn('POSTGRES_PASSWORD', actual['values'])

    def test_old_color_main_and_unprefixed_values_do_not_supply_missing_configuration(self):
        for prefix in ('', 'DEV_', 'DEV_BLUE_', 'DEV_GREEN_', 'AWS_', 'MAIN_'):
            values = {prefix + key.removeprefix('DEV_COM_'): value for key, value in secrets().items()}
            with self.subTest(prefix=prefix), self.assertRaises(infra.ConfigurationError):
                infra.build_settings(json.dumps(values), 'postgres')

    def test_missing_empty_or_space_required_values_are_rejected(self):
        for service, names in infra.SCHEMAS.items():
            for name in ('DATA_DOCKER_NETWORK', 'DATA_DOCKER_NETWORK_SUBNET') + names:
                for empty in (None, '', '   '):
                    values = secrets()
                    if empty is None:
                        del values['DEV_COM_' + name]
                    else:
                        values['DEV_COM_' + name] = empty
                    with self.subTest(service=service, key=name, empty=empty), self.assertRaises(infra.ConfigurationError):
                        infra.build_settings(json.dumps(values), service)

    def test_aliases_are_case_insensitive_but_collisions_are_rejected(self):
        values = {key.lower(): value for key, value in secrets().items()}
        self.assertEqual(infra.build_settings(json.dumps(values), 'postgres')['values']['POSTGRES_DB'], 'ieum')
        values['DEV_COM_POSTGRES_PASSWORD'] = SECRET
        with self.assertRaises(infra.ConfigurationError):
            infra.build_settings(json.dumps(values), 'postgres')
        payload = json.dumps(secrets())[:-1] + ',"DEV_COM_POSTGRES_PASSWORD":"duplicate"}'
        with self.assertRaises(infra.ConfigurationError):
            infra.build_settings(payload, 'postgres')

    def test_newline_control_characters_placeholders_and_nonstrings_are_rejected(self):
        for value in (SECRET + '\nNEXT=value', SECRET + '\r', SECRET + '\u2028', SECRET + '\x00',
                      '__FILL_FROM_CURRENT_DEV__', '<password>', None, 12, {'nested': SECRET}):
            with self.subTest(kind=type(value).__name__), self.assertRaises(infra.ConfigurationError):
                settings(changes={'DEV_COM_POSTGRES_PASSWORD': value})

    def test_malformed_oversized_and_nonobject_json_are_sanitized(self):
        for value in (None, '', '[]', 'null', '{', 'NaN', '{"x":NaN}', '[' * 1500,
                      json.dumps({**secrets(), 'IGNORED': 'x' * infra.MAX_BYTES})):
            with self.assertRaises(infra.ConfigurationError):
                infra.build_settings(value, 'postgres')

    def test_loopback_defaults_are_preserved_and_public_bindings_are_rejected(self):
        values = secrets()
        del values['DEV_COM_HOST_BIND_IP']
        self.assertEqual(infra.build_settings(json.dumps(values), 'redis')['values']['HOST_BIND_IP'], '127.0.0.1')
        for service in infra.SCHEMAS:
            self.assertEqual(settings(service, {'DEV_COM_HOST_BIND_IP': ''})['values']['HOST_BIND_IP'], '127.0.0.1')
            for value in ('0.0.0.0', '192.168.0.11', '::1', 'localhost'):
                with self.subTest(service=service, value=value), self.assertRaises(infra.ConfigurationError):
                    settings(service, {'DEV_COM_HOST_BIND_IP': value})

    def test_env_path_network_ips_and_ports_are_validated(self):
        invalid = {'POSTGRES_ENV_FILE': ('/tmp/postgres.env', '/home/kwung/deploy/ieum-infra/mongo.green.env',
                                         '/home/kwung/deploy/ieum-infra/sub/postgres.env'),
                   'DATA_DOCKER_NETWORK': ('host', 'ieum_net_green', 'has space'),
                   'DATA_DOCKER_NETWORK_SUBNET': ('8.8.8.0/24', '127.0.0.0/8', '10.30.10.3/24', 'fd00::/64'),
                   'POSTGRES_IP': ('10.10.10.13', '10.30.10.0', '10.30.10.255', 'invalid'),
                   'POSTGRES_CONTAINER_NAME': ('bad name', '-bad'),
                   'POSTGRES_PORT': ('5433', '0', '65536', '05432'),
                   'POSTGRES_HOST_PORT': ('0', '65536', '-1')}
        for key, values in invalid.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(infra.ConfigurationError):
                    settings(changes={'DEV_COM_' + key: value})

    def test_optional_host_port_defaults_and_overrides(self):
        self.assertEqual(settings('redis')['values']['REDIS_HOST_PORT'], '6379')
        self.assertEqual(settings('notion-mcp')['values']['NOTION_MCP_HOST_PORT'], '3000')
        self.assertEqual(settings('redis', {'DEV_COM_REDIS_HOST_PORT': '16379'})['values']['REDIS_HOST_PORT'], '16379')

    def test_selected_values_are_literal_and_not_trimmed_or_executed(self):
        value = '  서울 $VAR $(touch ignored) `echo x` \' " # = \\  '
        self.assertEqual(settings(changes={'DEV_COM_POSTGRES_PASSWORD': value})['values']['POSTGRES_PASSWORD'], value)

    def test_subprocess_environment_omits_json_secrets_and_ambient_compose_overrides(self):
        with patch.dict(os.environ, {**secrets(), infra.TRANSPORT: SECRET, 'POSTGRES_PASSWORD': SECRET,
                                    'COMPOSE_FILE': SECRET, 'COMPOSE_PROJECT_NAME': SECRET}):
            actual = infra.command_environment()
        for key in (*secrets(), infra.TRANSPORT, 'POSTGRES_PASSWORD', 'COMPOSE_FILE', 'COMPOSE_PROJECT_NAME'):
            self.assertNotIn(key, actual)
        self.assertEqual(actual['COMPOSE_DISABLE_ENV_FILE'], '1')

    def test_cli_consumes_explicit_inputs_and_discards_old_json_before_deployment(self):
        def observe(_settings):
            self.assertNotIn(infra.TRANSPORT, os.environ)
            self.assertTrue(all(name not in os.environ for name in infra.explicit_secret_names('postgres')))
            self.assertEqual(_settings, settings())
        output = io.StringIO()
        with patch.dict(os.environ, {**secrets(), infra.TRANSPORT: 'invalid-ignored-json'}, clear=True), \
                patch.object(infra, 'deploy', side_effect=observe), contextlib.redirect_stdout(output):
            self.assertEqual(infra.main(['--service', 'postgres', '--deploy']), 0)
        self.assertNotIn(SECRET, output.getvalue())

    def test_explicit_inputs_scope_each_service_and_preserve_optional_defaults(self):
        for service in infra.SCHEMAS:
            environment = {**secrets(), 'MAIN_COM_POSTGRES_PASSWORD': 'not-selected',
                           'AWS_POSTGRES_PASSWORD': 'not-selected', 'GITHUB_TOKEN': 'not-selected',
                           'DEV_COM_NEW_OPTION': 'not-selected'}
            environment.update({infra.PREFIX + key: '' for key in infra.OPTIONALS.get(service, {})})
            payload = infra.consume_explicit_secrets(environment, service)
            self.assertEqual(infra.build_settings(payload, service), settings(service))
            self.assertNotIn('not-selected', payload)
            self.assertTrue(all(name not in environment for name in infra.explicit_secret_names(service)))
            self.assertEqual(set(json.loads(payload)), set(infra.explicit_secret_names(service)))

    def test_cli_never_falls_back_to_whole_json_or_other_prefixes(self):
        for explicit in ({}, {'DEV_COM_POSTGRES_PASSWORD': ''}):
            other_prefixes = {prefix + key.removeprefix(infra.PREFIX): value
                              for prefix in ('MAIN_COM_', 'AWS_', '') for key, value in secrets().items()}
            out, err = io.StringIO(), io.StringIO()
            with patch.dict(os.environ, {**explicit, **other_prefixes, infra.TRANSPORT: json.dumps(secrets())}, clear=True), \
                    patch.object(infra, 'deploy') as mutate, \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = infra.main(['--service', 'postgres', '--deploy'])
            self.assertEqual(code, 1)
            mutate.assert_not_called()
            self.assertIn('missing_required_configuration', err.getvalue())
            self.assertNotIn(SECRET, out.getvalue() + err.getvalue())

    def test_real_check_cli_accepts_explicit_inputs_without_printing_values(self):
        for service in infra.SCHEMAS:
            environment = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG', 'LD_LIBRARY_PATH')
                           if key in os.environ}
            environment.update(secrets())
            environment[infra.TRANSPORT] = 'ignored-invalid-json'
            result = subprocess.run([sys.executable, str(infra.ROOT / 'scripts/dev/infra_env.py'),
                                     '--service', service], env=environment,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn(SECRET, result.stdout + result.stderr)

    def test_explicit_inputs_preserve_literal_values_and_validate_line_breaks(self):
        literal = '  서울 $VAR $(touch ignored) `echo x` " # = \\  '
        for value in (literal, literal + '\nINJECTED=yes'):
            environment = {**secrets(), 'DEV_COM_POSTGRES_PASSWORD': value}
            payload = infra.consume_explicit_secrets(environment, 'postgres')
            if '\n' in value:
                with self.assertRaises(infra.ConfigurationError):
                    infra.build_settings(payload, 'postgres')
            else:
                self.assertEqual(infra.build_settings(payload, 'postgres')['values']['POSTGRES_PASSWORD'], literal)


class LiveGuardTests(unittest.TestCase):
    def test_all_prepared_services_are_accepted(self):
        for service in infra.SCHEMAS:
            with patch.object(infra, 'inspect', side_effect=list(live(service))):
                infra.validate_live(settings(service))

    def test_migration_project_ip_and_volume_mismatch_stop_before_mutation(self):
        mutations = (
            lambda n, c: n.update(Driver='overlay'),
            lambda n, c: n.update(Internal=True),
            lambda n, c: n['IPAM']['Config'][0].update(Subnet='10.10.10.0/24'),
            lambda n, c: c['State'].update(Running=False),
            lambda n, c: c['NetworkSettings']['Networks'].update(ieum_net_green={}),
            lambda n, c: c['NetworkSettings']['Networks']['ieum_net_data'].update(IPAddress='10.30.10.99'),
            lambda n, c: c['Config'].update(Labels=None),
            lambda n, c: c['Mounts'][0].update(Name='a-new-empty-volume'),
            lambda n, c: c['Mounts'][0].update(Type='bind'),
        )
        for mutate in mutations:
            network, container = live()
            mutate(network, container)
            with patch.object(infra, 'inspect', side_effect=[network, container]), self.assertRaises(infra.ConfigurationError):
                infra.validate_live(settings())


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.root.chmod(0o700)
        self.target = self.root / 'postgres.green.env'
        self.target.write_bytes(b'old private configuration\n')
        self.target.chmod(0o600)
        self.selected = settings()
        self.selected['path'] = self.target

    def test_success_uses_same_project_and_no_volume_or_network_deletion(self):
        commands = []
        with patch.object(infra, 'ENV_HOME', self.root), \
                patch.object(infra, 'deployment_lock', contextlib.nullcontext), \
                patch.object(infra, 'validate_live') as guard, \
                patch.object(infra, 'run', side_effect=lambda arguments, **_: commands.append(arguments) or ''), \
                patch.object(infra, 'inspect', return_value=live()[1]):
            infra.deploy(self.selected)
        self.assertEqual(guard.call_count, 2)
        self.assertEqual(len(commands), 2)
        self.assertEqual(commands[0][-2:], ['config', '--quiet'])
        self.assertEqual(commands[1][-6:], ['up', '-d', '--no-deps', '--pull', 'never', 'postgres'])
        for arguments in commands:
            self.assertIn('ieum-green-postgres', arguments)
            self.assertFalse(set(arguments) & {'down', '-v', 'rm', '--remove-orphans', 'create'})
        self.assertEqual(self.target.read_bytes(), infra.compose_environment(self.selected['values']))
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)

    def test_preflight_failure_preserves_old_file_and_does_not_run_compose(self):
        with patch.object(infra, 'deployment_lock', contextlib.nullcontext), \
                patch.object(infra, 'validate_live', side_effect=infra.ConfigurationError('blocked')), \
                patch.object(infra, 'run') as command:
            with self.assertRaises(infra.ConfigurationError):
                infra.deploy(self.selected)
        command.assert_not_called()
        self.assertEqual(self.target.read_bytes(), b'old private configuration\n')

    def test_compose_failure_restores_old_file_without_deleting_data(self):
        def fail(arguments, **_):
            if 'up' in arguments:
                raise infra.ConfigurationError('docker_command_failed')
            return ''
        with patch.object(infra, 'ENV_HOME', self.root), \
                patch.object(infra, 'deployment_lock', contextlib.nullcontext), \
                patch.object(infra, 'validate_live'), patch.object(infra, 'run', side_effect=fail):
            with self.assertRaises(infra.ConfigurationError):
                infra.deploy(self.selected)
        self.assertEqual(self.target.read_bytes(), b'old private configuration\n')
        self.assertEqual(list(self.root.glob('.infra-env-*')), [])

    def test_existing_public_env_and_symlink_are_rejected(self):
        with patch.object(infra, 'ENV_HOME', self.root):
            self.target.chmod(0o644)
            with self.assertRaises(infra.ConfigurationError):
                infra.existing_environment(self.target)
            self.target.unlink()
            self.target.symlink_to(self.root / 'elsewhere')
            with self.assertRaises(OSError):
                infra.existing_environment(self.target)

    def test_existing_shared_lock_is_used_without_truncation(self):
        path = self.root / 'deploy.lock'
        path.write_bytes(b'unchanged-lock-content')
        path.chmod(0o600)
        with patch.object(infra, 'LOCK_FILE', path), infra.deployment_lock():
            self.assertEqual(path.read_bytes(), b'unchanged-lock-content')
        self.assertEqual(path.read_bytes(), b'unchanged-lock-content')

    def test_infra_and_existing_app_controller_exclude_each_other(self):
        path = self.root / 'deploy.lock'
        path.touch(mode=0o600)
        spec = importlib.util.spec_from_file_location('core_lock_interop', infra.ROOT / 'bluegreen/bluegreen.py')
        core = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(core)
        with patch.object(core, 'HOME', self.root), patch.object(infra, 'LOCK_FILE', path):
            with infra.deployment_lock():
                with self.assertRaises(RuntimeError):
                    with core.deployment_lock(0, 'infra-test', read_only=True):
                        self.fail('App controller acquired Infra-held lock')
            with core.deployment_lock(0, 'infra-test', read_only=True):
                with patch.object(infra.time, 'monotonic', side_effect=[0, 301]):
                    with self.assertRaises(infra.ConfigurationError):
                        with infra.deployment_lock():
                            self.fail('Infra acquired app-held lock')


class WorkflowTests(unittest.TestCase):
    def test_dev_workflows_declare_only_service_inputs_and_keep_manual_execution(self):
        files = ('postgres-green-reset.yml', 'mongo-green-reset.yml', 'redis-green-reset.yml',
                 'phoenix-green-reset.yml', 'notion-mcp-green.yml')
        for service, name in zip(infra.SCHEMAS, files):
            source = (infra.ROOT / '.github/workflows' / name).read_text()
            self.assertNotIn('toJSON(secrets)', source)
            self.assertNotIn(infra.TRANSPORT, source)
            selected = re.findall(r'^          (DEV_COM_[A-Z0-9_]+): \$\{\{ secrets\.([A-Z0-9_]+) \}\}$',
                                  source, re.MULTILINE)
            self.assertEqual({name for name, _ in selected}, set(infra.explicit_secret_names(service)))
            self.assertEqual(len(selected), len(infra.explicit_secret_names(service)))
            self.assertTrue(all(name == secret for name, secret in selected))
            self.assertEqual(len(re.findall(r'secrets\.', source)), len(selected))
            self.assertIn('workflow_dispatch:', source)
            self.assertNotIn('  push:', source)
            self.assertNotIn('down -v', source)
            self.assertNotIn('docker rm', source)
            self.assertIn('set +x', source)
            self.assertIn('infra_dev_shared_services', source)


class ComposeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which('docker') is None:
            raise unittest.SkipTest('Docker CLI unavailable; no daemon is required')
        result = subprocess.run(['docker', 'compose', 'version'], capture_output=True, timeout=10)
        if result.returncode:
            raise unittest.SkipTest('Docker Compose CLI unavailable')

    def render(self, selected):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'private.env'
            path.write_bytes(infra.compose_environment(selected['values']))
            path.chmod(0o600)
            command = ['docker', 'compose', '--project-name', selected['project'],
                       '--env-file', str(path), '-f', str(infra.ROOT / 'compose' /
                       (selected['service'] + '.green.compose.yml')), 'config', '--format', 'json']
            result = subprocess.run(command, env=infra.command_environment(), capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, 'Offline Compose configuration rejected')
            return json.loads(result.stdout)

    def test_five_models_keep_existing_volume_names_and_localhost_ports(self):
        for service in infra.SCHEMAS:
            selected = settings(service)
            model = self.render(selected)
            actual = model['services'][service]
            self.assertEqual(model['networks']['green_network']['name'], 'ieum_net_data')
            self.assertTrue(all(port['host_ip'] == '127.0.0.1' for port in actual['ports']))
            if service in infra.VOLUMES:
                logical, _ = infra.VOLUMES[service]
                self.assertEqual(model['volumes'][logical]['name'], selected['project'] + '_' + logical)

    def test_password_special_characters_survive_compose_without_entering_health_shell(self):
        for service, key in (('postgres', 'POSTGRES_PASSWORD'), ('mongo', 'MONGO_INITDB_ROOT_PASSWORD')):
            for literal in ("  $VAR${OTHER}$$ # = \' \" \\ 서울  ", 'trailing\\', '$(touch ignored)`echo x`'):
                selected = settings(service, {'DEV_COM_' + key: literal})
                actual = self.render(selected)['services'][service]
                # Compose config re-escapes literal dollars for re-serialization.
                self.assertEqual(actual['environment'][key].replace('$$', '$'), literal)
                self.assertNotIn(literal, ' '.join(actual['healthcheck']['test']))


if __name__ == '__main__':
    unittest.main()
