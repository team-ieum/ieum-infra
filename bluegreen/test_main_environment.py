"""DEV/MAIN boundary and first production release; no Docker/server access."""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


def load(filename, environment='main'):
    with patch.dict(os.environ, {'IEUM_DEPLOY_ENVIRONMENT': environment}):
        spec = importlib.util.spec_from_file_location('main_' + filename.replace('-', '_'), Path(__file__).with_name(filename))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


def policy(helper):
    data = {'version': 2, 'data': {'network': 'ieum_main_net_data', 'subnet': '10.60.10.0/24',
        'stores': {kind: 'ieum-main-' + kind for kind in ('postgres', 'redis', 'mongo')}}}
    for color, subnet in [('blue', '10.40.10'), ('green', '10.50.10')]:
        directory = helper.RUNTIME_ROOT / color
        data[color] = {'network': 'ieum_main_net_' + color, 'subnet': subnet + '.0/24',
            'containerName': 'ieum-main-' + color + '-frontend', 'ip': subnet + '.20',
            'backendIp': subnet + '.21', 'agentIp': subnet + '.24',
            'envDir': str(directory), 'envFile': str(directory / 'frontend.env')}
    return data


class MainEnvironmentTest(unittest.TestCase):
    def setUp(self):
        self.bg = load('bluegreen.py')
        self.helper = load('nginx-helper.py')
        self.helper.configure_environment('main')
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.bg.HOME = self.home
        self.bg.ROUTE_CONFIG = self.home / 'route.conf'
        self.bg.FRONTEND_POLICY = self.home / 'policy.json'
        self.bg.ASSETS = self.home / 'assets'
        self.bg.FRONTEND_SETTINGS = policy(self.helper)
        for color in ('blue', 'green'):
            self.bg.IPS[color] = {app: self.bg.FRONTEND_SETTINGS[color][key]
                for app, key in [('frontend', 'ip'), ('backend', 'backendIp'), ('agent', 'agentIp')]}
            directory = self.home / 'runtime' / color
            self.bg.FRONTEND_SETTINGS[color].update(envDir=str(directory), envFile=str(directory / 'frontend.env'))
        self.bg.atomic(self.bg.ROUTE_CONFIG, '# Managed by ieum-bluegreen-nginx. active=legacy\n')
        self.bg.atomic(self.home / 'control/active', 'legacy\n')
        self.bg.atomic(self.home / 'control/phase', 'ready\n')
        self.bg.atomic(self.home / 'control/resource-limits.env', 'LIMITS_JSON=' + json.dumps({
            app: {'cpus': 1, 'memoryMb': 512, 'pids': 128} for app in self.bg.APPS}) + '\n')
        self.bg.save({'active': 'legacy', 'previous': None, 'slots': {}, 'freshBootstrap': True})

    def test_main_paths_and_names_never_resolve_to_dev(self):
        bg = load('bluegreen.py')
        self.assertEqual(str(bg.HOME), '/home/kwung/deploy/bluegreen-main')
        self.assertEqual(bg.name('blue', 'backend'), 'ieum-main-blue-backend')
        self.assertEqual(bg.NGINX_HELPER, '/usr/local/sbin/ieum-main-bluegreen-nginx')
        self.assertEqual(bg.INGRESS_HOST, 'kiacms.co.kr')
        self.assertIn('main', str(bg.FRONTEND_POLICY))
        with patch.object(bg.os.path, 'lexists', return_value=False):
            with self.assertRaises(RuntimeError): bg.load_frontend_settings()
        with self.assertRaises(RuntimeError): bg.data_store_name('postgres')

    def test_invalid_environment_is_rejected(self):
        with self.assertRaises(RuntimeError): load('bluegreen.py', 'production')

    def test_existing_dev_defaults_are_preserved(self):
        bg = load('bluegreen.py', 'dev')
        self.assertEqual(bg.name('green', 'backend'), 'ieum-green-backend')
        self.assertEqual(bg.INGRESS_HOST, 'green.kiacms.co.kr')
        self.assertTrue(bg.owns_environment({}))
        self.assertFalse(bg.owns_environment({'ieum.bluegreen.environment': 'main'}))
        self.assertEqual(bg.main_resource_options('backend'), [])

    def test_main_requires_explicit_main_ownership(self):
        for labels in [{}, {'ieum.bluegreen.environment': 'dev'}]:
            self.assertFalse(self.bg.owns_environment(labels))
        self.assertTrue(self.bg.owns_environment({'ieum.bluegreen.environment': 'main'}))

    def test_foreign_state_is_refused_without_rewriting(self):
        path = self.home / 'state.json'
        path.write_text('{"active":"blue","slots":{}}')
        before = path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'another environment'): self.bg.state()
        self.assertEqual(path.read_bytes(), before)

    def test_foreign_recovery_journal_never_changes_a_container(self):
        path = self.home / 'transaction.json'
        path.write_text('{"from":"blue","to":"green"}')
        with patch.object(self.bg, 'docker') as docker:
            with self.assertRaisesRegex(RuntimeError, 'another environment'): self.bg.recover()
        docker.assert_not_called()
        self.assertTrue(path.exists())

    def test_foreign_inactive_container_is_not_drained_or_removed(self):
        with patch.object(self.bg, 'exists', side_effect=lambda name: '-offline-' not in name), \
             patch.object(self.bg, 'inspect', return_value={'Config': {'Labels': {
                 'ieum.bluegreen.managed': 'true', 'ieum.bluegreen.environment': 'dev'}}}), \
             patch.object(self.bg, 'drain') as drain, patch.object(self.bg, 'docker') as docker:
            with self.assertRaisesRegex(RuntimeError, 'foreign'): self.bg.remove_inactive('blue')
        drain.assert_not_called(); docker.assert_not_called()

    def test_first_prepare_requires_all_images_and_private_snapshots(self):
        with patch.object(self.bg, 'docker') as docker:
            with self.assertRaisesRegex(RuntimeError, 'all three'):
                self.bg.prepare('blue', {'frontend': 'image'}, {'frontend': 'env'}, replace_env=True)
        docker.assert_not_called()

    def test_initialization_never_reads_dev_legacy_containers(self):
        (self.home / 'state.json').unlink()
        with patch.object(self.bg, 'exists', return_value=False), \
             patch.object(self.bg, 'validate_split_networks'), patch.object(self.bg, 'inspect') as inspect:
            with contextlib.redirect_stdout(io.StringIO()): self.bg.init()
        inspect.assert_not_called()
        self.assertTrue(self.bg.state()['freshBootstrap'])
        self.assertEqual((self.home / 'control/phase').read_text(), 'ready\n')
        self.assertFalse((self.home / 'snapshots/legacy.json').exists())

    def test_staging_saves_private_env_and_digest_without_container_changes(self):
        source = self.home / 'input.env'; source.write_text('TOKEN=synthetic-only\n'); source.chmod(0o600)
        with patch.object(self.bg, 'docker', return_value=json.dumps([{'Id': 'sha256:' + 'a' * 64}])) as docker:
            with contextlib.redirect_stdout(io.StringIO()): self.bg.stage_bootstrap('agent', 'image', source)
        self.assertEqual(docker.call_count, 1)
        self.assertEqual(docker.call_args.args[:2], ('image', 'inspect'))
        self.assertEqual((self.home / 'bootstrap/agent.env').stat().st_mode & 0o777, 0o600)
        self.assertNotIn('synthetic-only', (self.home / 'bootstrap/agent.json').read_text())

    def test_staging_after_activation_is_rejected(self):
        self.bg.save({'active': 'blue', 'previous': None, 'slots': {}})
        with patch.object(self.bg, 'docker') as docker:
            with self.assertRaisesRegex(RuntimeError, 'after preparation'):
                self.bg.stage_bootstrap('agent', 'image', 'unused')
        docker.assert_not_called()

    def test_modified_bootstrap_env_cannot_be_prepared(self):
        directory = self.home / 'bootstrap'; directory.mkdir()
        app = self.bg.APPS[0]
        file = directory / (app + '.env'); file.write_text('CHANGED=value\n'); file.chmod(0o600)
        (directory / (app + '.json')).write_text(json.dumps({'service': app, 'environment': 'main',
            'image': 'sha256:' + 'a' * 64, 'envSha256': '0' * 64}))
        with patch.object(self.bg, 'prepare') as prepare:
            with self.assertRaisesRegex(RuntimeError, 'identity changed'):
                self.bg.prepare_bootstrap('blue', '2026-11-10T00:00:00Z', 'Etc/UTC')
        prepare.assert_not_called()

    def test_resource_limits_are_required_and_enforced(self):
        args = self.bg.main_resource_options('backend')
        self.assertIn('--memory', args); self.assertIn('--pids-limit', args)
        (self.home / 'control/resource-limits.env').unlink()
        with self.assertRaises(FileNotFoundError): self.bg.main_resource_options('backend')

    def test_first_main_release_uses_only_staged_images_and_main_capped_containers(self):
        bg = self.bg
        environments = {'frontend': {'VITE_API_URL': 'https://kiacms.co.kr'},
            'backend': {'DB_URL': 'jdbc:postgresql://ieum-main-postgres:5432/ieum_main',
                'REDIS_HOST': 'ieum-main-redis', 'MONGO_URI': 'mongodb://ieum-main-mongo:27017/ieum_main',
                'CORS_ALLOWED_ORIGINS': 'https://kiacms.co.kr', 'SPRING_PROFILES_ACTIVE': 'main',
                'AGENT_INTERNAL_TOKEN': 'synthetic-test-only'},
            'agent': {'MONGODB_URL': 'mongodb://ieum-main-mongo:27017/ieum_main',
                'AGENT_INTERNAL_TOKEN': 'synthetic-test-only'}}
        files = {}
        for app, values in environments.items():
            path = self.home / (app + '.env')
            bg.atomic(path, ''.join(k + '=' + v + '\n' for k, v in values.items()))
            files[app] = str(path)
        images = {app: 'sha256:' + str(index + 1) * 64 for index, app in enumerate(bg.APPS)}
        created = []
        def docker(*args):
            if args[:2] == ('image', 'inspect'):
                return json.dumps([{'Id': args[2], 'Config': {'Labels': {
                    'ieum.bluegreen.capable': 'true', 'ieum.bluegreen.protocol': '2'}}}])
            if args[0] == 'create':
                created.append(args); return str(len(created)) * 64
            if args[0] == 'start' or args[:2] == ('network', 'connect'): return ''
            raise AssertionError(args)
        stores = {kind: {'name': 'ieum-main-' + kind, 'ip': '10.60.10.' + str(index + 10)}
                  for index, kind in enumerate(('postgres', 'redis', 'mongo'))}
        with patch.object(bg, 'docker', side_effect=docker), patch.object(bg, 'exists', return_value=False), \
                patch.object(bg, 'validate_frontend_network', return_value=stores), \
                patch.object(bg, 'capture_legacy_snapshot') as legacy, \
                patch.object(bg, 'verify_legacy_snapshot') as verify_legacy, \
                patch.object(bg, 'check', return_value={'draining': True, 'schedulerStandby': True}):
            with contextlib.redirect_stdout(io.StringIO()):
                bg.prepare('blue', images, files, replace_env=True,
                           recovery_not_before='2026-11-10T00:00:00Z', backend_zone='Etc/UTC')
        legacy.assert_not_called(); verify_legacy.assert_not_called()
        self.assertEqual(len(created), 3)
        for args in created:
            self.assertTrue(args[args.index('--name') + 1].startswith('ieum-main-blue-'))
            self.assertIn('ieum.bluegreen.environment=main', args)
            self.assertIn('--memory', args)
            self.assertIn('ieum_main_net_blue', args)
        self.assertEqual(bg.state()['active'], 'legacy')
        self.assertEqual(bg.state()['slots']['blue']['status'], 'candidate')
        backend = bg.read_env(self.home / 'runtime/blue/backend.env')
        self.assertEqual(backend['SPRING_JPA_HIBERNATE_DDL_AUTO'], 'validate')
        self.assertEqual(backend['AGENT_SERVICE_URL'], 'http://10.40.10.24:8000')

    def test_main_cross_environment_service_endpoints_and_token_mismatch_are_rejected(self):
        stores = {kind: {'name': 'ieum-main-' + kind, 'ip': '10.60.10.' + str(index + 10)}
                  for index, kind in enumerate(('postgres', 'redis', 'mongo'))}
        env = {'frontend': {'VITE_API_URL': 'https://kiacms.co.kr'},
               'backend': {'DB_URL': 'jdbc:postgresql://ieum-main-postgres:5432/ieum_main',
                   'REDIS_HOST': 'ieum-main-redis', 'MONGO_URI': 'mongodb://ieum-main-mongo:27017/ieum_main',
                   'CORS_ALLOWED_ORIGINS': 'https://kiacms.co.kr', 'SPRING_PROFILES_ACTIVE': 'main',
                   'AGENT_INTERNAL_TOKEN': 'test-token'},
               'agent': {'MONGODB_URL': 'mongodb://ieum-main-mongo:27017/ieum_main', 'AGENT_INTERNAL_TOKEN': 'test-token'}}
        self.bg.validate_data_environment(env, stores)
        for app, key, value in [('backend', 'REDIS_HOST', 'ieum-redis'),
                ('agent', 'AGENT_INTERNAL_TOKEN', 'different'),
                ('backend', 'AGENT_INTERNAL_TOKEN', ' test-token '),
                ('agent', 'AGENT_INTERNAL_TOKEN', ' test-token '),
                ('agent', 'OTEL_EXPORTER_OTLP_ENDPOINT', 'http://ieum-green-phoenix:6006/v1/traces'),
                ('agent', 'NOTION_MCP_URL', 'http://ieum-notion-mcp:3000/mcp')]:
            changed = copy.deepcopy(env); changed[app][key] = value
            with self.assertRaises(RuntimeError): self.bg.validate_data_environment(changed, stores)

    def test_offline_rollback_checks_main_503_without_touching_dev_apps(self):
        result = SimpleNamespace(returncode=0, stdout='HTTP/2 503\r\nX-IEUM-Environment: legacy\r\nX-IEUM-Deployment: main\r\n')
        with patch.object(self.bg.subprocess, 'run', return_value=result) as run, \
             patch.object(self.bg, 'inspect') as inspect:
            self.bg.verify_ingress('legacy')
        inspect.assert_not_called()
        self.assertIn('https://kiacms.co.kr:11111/', run.call_args.args[0])

    def test_main_ingress_rejects_same_color_response_from_other_site(self):
        for marker in ('', 'X-IEUM-Deployment: dev\r\n',
                       'X-IEUM-Deployment: main\r\nX-IEUM-Deployment: main\r\n'):
            result = SimpleNamespace(returncode=0, stdout='HTTP/2 200\r\nX-IEUM-Environment: blue\r\n' + marker)
            with patch.object(self.bg.subprocess, 'run', return_value=result), \
                 patch.object(self.bg, 'inspect', return_value={'Config': {'Env': []}}), \
                 patch.object(self.bg.time, 'monotonic', side_effect=[0, 0, 2]), \
                 patch.object(self.bg.time, 'sleep'):
                with self.assertRaisesRegex(RuntimeError, 'TLS ingress verification failed'):
                    self.bg.verify_ingress('blue', timeout=1)

    def test_main_ingress_checks_all_three_routes_and_main_identity(self):
        result = SimpleNamespace(returncode=0, stdout='HTTP/2 200\r\nX-IEUM-Environment: blue\r\nX-IEUM-Deployment: main\r\n')
        with patch.object(self.bg.subprocess, 'run', return_value=result) as run, \
             patch.object(self.bg, 'inspect', return_value={'Config': {'Env': []}}):
            self.bg.verify_ingress('blue')
        self.assertEqual(run.call_count, 3)
        self.assertEqual([call.args[0][-1] for call in run.call_args_list], [
            'https://kiacms.co.kr:11111/', 'https://kiacms.co.kr:11111/v3/api-docs',
            'https://kiacms.co.kr:11111/openapi.json'])

    def test_main_rejects_dev_topology_and_names(self):
        correct = policy(self.helper)
        self.assertEqual(self.helper.validate_frontend_settings(correct), correct)
        for key, replacement in [('network', 'ieum_net_blue'), ('containerName', 'ieum-blue-frontend'),
                ('envDir', '/home/kwung/deploy/bluegreen/runtime/blue')]:
            wrong = copy.deepcopy(correct); wrong['blue'][key] = replacement
            with self.assertRaises(ValueError): self.helper.validate_frontend_settings(wrong)
        wrong = copy.deepcopy(correct); wrong['data']['stores']['mongo'] = 'ieum-mongo'
        with self.assertRaises(ValueError): self.helper.validate_frontend_settings(wrong)

    def test_main_nginx_symbols_and_offline_route_are_independent(self):
        with patch.object(self.helper, 'frontend_policy', return_value=policy(self.helper)):
            active = self.helper.render('blue'); offline = self.helper.render('legacy')
        self.assertIn('upstream ieum_main_backend', active)
        self.assertNotIn('upstream ieum_backend ', active)
        self.assertIn('$ieum_main_connection_upgrade', active)
        self.assertIn('127.0.0.1:9', offline)
        self.assertNotIn('10.10.10.', offline)

    def test_main_render_without_policy_cannot_use_dev_default_addresses(self):
        with patch.object(self.helper, 'frontend_policy', return_value=None):
            with self.assertRaises(ValueError): self.helper.render('blue')

    def test_main_site_derivation_keeps_tls_waf_and_changes_only_its_routes(self):
        installer = load('install-nginx.py')
        original = '''server {
    listen 11111 ssl default_server;
    server_name green.kiacms.co.kr;
    ssl_certificate /etc/letsencrypt/live/kiacms.co.kr/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/kiacms.co.kr/privkey.pem;
    modsecurity on;
    modsecurity_rules_file /reviewed/waf.conf;
    add_header X-Content-Type-Options nosniff always;
    location /api/ {
        proxy_pass http://10.10.10.11:8080;
    }
    location / {
        proxy_pass http://10.10.10.10;
    }
}
'''
        rendered = installer.render_main_site(original)
        self.assertIn('server_name kiacms.co.kr;', rendered)
        self.assertIn('add_header X-IEUM-Deployment main always;', rendered)
        self.assertIn('modsecurity_rules_file /reviewed/waf.conf;', rendered)
        self.assertIn('http://ieum_main_backend', rendered)
        self.assertIn('http://ieum_main_frontend', rendered)
        self.assertIn('return 503', rendered)
        self.assertNotIn('default_server', rendered)
        self.assertNotIn('green.kiacms.co.kr', rendered)
        self.assertIn('server_name green.kiacms.co.kr;', original)

    def test_shared_nginx_lock_serializes_both_helpers(self):
        import fcntl
        path = self.home / 'nginx.lock'
        self.helper.NGINX_LOCK = path
        real_stat = os.fstat
        def root_stat(fd):
            info = real_stat(fd)
            return SimpleNamespace(st_mode=info.st_mode, st_uid=0, st_nlink=info.st_nlink)
        with patch.object(self.helper.os, 'fstat', side_effect=root_stat):
            with self.helper.nginx_lock():
                with path.open('r') as other:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with path.open('r') as other:
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_shared_nginx_lock_rejects_symlink_without_touching_target(self):
        target = self.home / 'target'; target.write_text('preserved')
        path = self.home / 'nginx.lock'; path.symlink_to(target)
        self.helper.NGINX_LOCK = path
        with self.assertRaises(OSError):
            with self.helper.nginx_lock(): self.fail('unsafe lock')
        self.assertEqual(target.read_text(), 'preserved')

    def test_installed_helper_filename_selects_main_without_trusting_caller_environment(self):
        code = Path(__file__).with_name('nginx-helper.py').read_text()
        namespace = {'__file__': '/usr/local/sbin/ieum-main-bluegreen-nginx', '__name__': 'test_helper'}
        with patch.dict(os.environ, {'IEUM_DEPLOY_ENVIRONMENT': 'dev'}): exec(compile(code, namespace['__file__'], 'exec'), namespace)
        self.assertEqual(namespace['ENVIRONMENT'], 'main')
        self.assertEqual(namespace['DESTINATION'], Path('/etc/nginx/conf.d/ieum-main-bluegreen.conf'))


if __name__ == '__main__':
    unittest.main()
