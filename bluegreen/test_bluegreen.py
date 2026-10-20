import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('bg', Path(__file__).with_name('bluegreen.py'))
bg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bg)

def images(color):
    return {app: 'sha256:' + color + '-' + app for app in bg.APPS}

def details(**overrides):
    return dict(protocolVersion=2, clusteredQuartz=True, draining=True, schedulerStandby=True,
                activeJobs=0, queuedJobs=0, activeRequests=0, sseSubscribers=0, websocketUsers=0,
                **overrides)

class DeploymentSafetyTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for attribute, value in [('HOME', self.root), ('ROUTE_CONFIG', self.root / 'nginx.conf'),
                                 ('ASSETS', self.root / 'assets')]:
            p = patch.object(bg, attribute, value)
            p.start(); self.addCleanup(p.stop)
        bg.save({'active': 'green', 'previous': 'blue', 'slots': {
            color: {'status': 'active' if color == 'green' else 'retained',
                    'images': images(color), 'release': bg.release_id(images(color))}
            for color in ('blue', 'green')}})
        bg.atomic(bg.HOME / 'control/active', 'green\n')
        bg.atomic(bg.HOME / 'control/phase', 'ready\n')
        self.set_route('green')
        bg.atomic(bg.HOME / 'snapshots/legacy.json', json.dumps({
            app: {'image': 'legacy-' + app, 'environment': {}} for app in bg.APPS}))
        for attribute, kwargs in [
            ('exists', {'return_value': False}), ('slot_images', {'side_effect': images}),
            ('archive_frontend', {}), ('check', {'return_value': details()}),
            ('verify_ingress', {}), ('control', {'return_value': details()})]:
            p = patch.object(bg, attribute, **kwargs)
            p.start(); self.addCleanup(p.stop)

    def set_route(self, color):
        bg.atomic(bg.ROUTE_CONFIG, '# Managed by ieum-bluegreen-nginx. active=' + color + '\n')

    def test_unhealthy_candidate_never_drains_active_or_changes_routing(self):
        with patch.object(bg, 'check', side_effect=RuntimeError('unhealthy')), patch.object(bg, 'route') as route:
            with self.assertRaisesRegex(RuntimeError, 'unhealthy'): bg.switch('blue')
            route.assert_not_called()
        bg.control.assert_not_called()
        self.assertEqual(bg.state()['active'], 'green')
        self.assertFalse((bg.HOME / 'transaction.json').exists())

    def test_failed_probe_restores_and_verifies_previous_route(self):
        with patch.object(bg, 'route', side_effect=self.set_route) as route, \
             patch.object(bg, 'verify_ingress', side_effect=[RuntimeError('probe'), None]) as verify:
            with self.assertRaisesRegex(RuntimeError, 'probe'): bg.switch('blue')
        self.assertEqual([c.args[0] for c in route.call_args_list], ['blue', 'green'])
        self.assertEqual([c.args[0] for c in verify.call_args_list], ['blue', 'green'])
        self.assertEqual(bg.state()['active'], 'green')
        self.assertIsNone(bg.state()['previous'])
        self.assertEqual(bg.state()['slots']['blue']['status'], 'failed')
        self.assertFalse((bg.HOME / 'transaction.json').exists())

    def test_restore_failure_keeps_journal_and_original_cause(self):
        original = RuntimeError('original probe failure')
        def route(color):
            if color == 'green': raise OSError('restore failure')
            self.set_route(color)
        with patch.object(bg, 'route', side_effect=route), \
             patch.object(bg, 'verify_ingress', side_effect=original):
            with self.assertRaisesRegex(RuntimeError, 'rollback is incomplete') as caught:
                bg.switch('blue')
        self.assertIs(caught.exception.__cause__, original)
        self.assertTrue((bg.HOME / 'transaction.json').exists())
        self.assertIn('switch_failed_recovery_required', (bg.HOME / 'history.jsonl').read_text())

    def test_successful_switch_drains_previous_before_resuming_candidate(self):
        with patch.object(bg, 'route', side_effect=self.set_route): bg.switch('blue')
        self.assertEqual([c.args for c in bg.control.call_args_list], [('green', 'drain'), ('blue', 'resume')])
        self.assertEqual(bg.state()['active'], 'blue')
        self.assertEqual(bg.state()['slots']['green']['status'], 'retained')
        self.assertFalse((bg.HOME / 'transaction.json').exists())

    def test_candidate_asset_url_collision_is_rejected_before_draining_or_routing(self):
        with patch.object(bg, 'archive_frontend', side_effect=[None, RuntimeError('asset URL reused')]), \
             patch.object(bg, 'route') as route:
            with self.assertRaisesRegex(RuntimeError, 'asset URL reused'): bg.switch('blue')
        route.assert_not_called()
        bg.control.assert_not_called()
        self.assertFalse((bg.HOME / 'transaction.json').exists())

    def test_recover_finalizes_already_committed_switch_instead_of_rolling_back(self):
        original = bg.state()
        data = bg.state(); data['active'] = 'blue'; data['previous'] = 'green'
        data['slots']['blue']['status'] = 'active'; data['slots']['green']['status'] = 'retained'
        bg.save(data); self.set_route('blue'); bg.atomic(bg.HOME / 'control/active', 'blue\n')
        bg.atomic(bg.HOME / 'transaction.json', json.dumps({
            'from': 'green', 'to': 'blue', 'snapshot': original, 'targetImages': images('blue')}))
        with patch.object(bg, 'route') as route: bg.recover()
        route.assert_not_called()
        self.assertEqual(bg.state()['active'], 'blue')
        self.assertFalse((bg.HOME / 'transaction.json').exists())

    def test_recover_without_transaction_is_noop(self):
        with patch.object(bg, 'route') as route: bg.recover()
        route.assert_not_called()

    def test_active_environment_cannot_be_replaced(self):
        with patch.object(bg, 'docker') as docker:
            with self.assertRaisesRegex(RuntimeError, 'active environment'): bg.remove_inactive('green')
        docker.assert_not_called()

    def test_manual_nginx_route_drift_blocks_container_removal(self):
        self.set_route('blue')
        with patch.object(bg, 'docker') as docker:
            with self.assertRaisesRegex(RuntimeError, 'disagree'): bg.remove_inactive('blue')
        docker.assert_not_called()

    def test_running_legacy_blocks_new_changes(self):
        with patch.object(bg, 'exists', side_effect=lambda n: n == 'ieum-backend'), \
             patch.object(bg, 'inspect', return_value={'State': {'Status': 'running'}}):
            with self.assertRaisesRegex(RuntimeError, 'Legacy application'): bg.assert_consistent()

    def test_replacement_invalidates_previous_before_any_destructive_call(self):
        observed = []
        def docker(*args):
            observed.append((bg.state()['previous'], bg.state()['slots']['blue']['status']))
            raise RuntimeError('stop failed')
        with patch.object(bg, 'exists', side_effect=lambda n: n.startswith('ieum-blue-')), \
             patch.object(bg, 'inspect', return_value={'Config': {'Labels': {'ieum.bluegreen.managed': 'true'}},
                                                     'State': {'Status': 'running'}}), \
             patch.object(bg, 'drain'), patch.object(bg, 'docker', side_effect=docker):
            with self.assertRaisesRegex(RuntimeError, 'stop failed'): bg.remove_inactive('blue')
        self.assertEqual(observed, [(None, 'preparing')])

    def test_failed_preparation_is_never_a_rollback_release(self):
        def docker(*args):
            if args[:2] == ('image', 'inspect'):
                return json.dumps([{'Id': args[2], 'Config': {'Labels': {
                    'ieum.bluegreen.capable': 'true', 'ieum.bluegreen.protocol': '2'}}}])
            raise RuntimeError('candidate creation failed')
        with patch.object(bg, 'inspect', side_effect=lambda n: {'Image': images('green')[n.rsplit('-', 1)[1]]}), \
             patch.object(bg, 'env_values', return_value={}), \
             patch.object(bg, 'remove_inactive', side_effect=lambda c: bg.invalidate_slot(c)), \
             patch.object(bg, 'docker', side_effect=docker):
            with self.assertRaisesRegex(RuntimeError, 'creation failed'): bg.prepare('blue', {'backend': 'new-backend'})
        self.assertEqual(bg.state()['slots']['blue']['status'], 'failed')
        self.assertIsNone(bg.state()['previous'])
        with self.assertRaisesRegex(RuntimeError, 'No retained'): bg.rollback(bg.release_id(images('blue')))

    def test_protocol_one_image_is_rejected_before_replacing_any_container(self):
        with patch.object(bg, 'inspect', side_effect=lambda n: {'Image': images('green')[n.rsplit('-', 1)[1]]}), \
             patch.object(bg, 'docker', return_value=json.dumps([{'Id': 'old', 'Config': {'Labels': {
                 'ieum.bluegreen.capable': 'true'}}}])), patch.object(bg, 'remove_inactive') as remove:
            with self.assertRaisesRegex(RuntimeError, 'protocol-2'): bg.prepare('blue', {})
        remove.assert_not_called()

    def test_rollback_requires_expected_release_and_retained_status(self):
        with patch.object(bg, 'switch') as switch:
            with self.assertRaisesRegex(RuntimeError, 'exact retained'): bg.rollback('incorrect')
            switch.assert_not_called()
            bg.rollback(bg.release_id(images('blue')))
            switch.assert_called_once_with('blue')
        invalidate = bg.state(); invalidate['slots']['blue']['status'] = 'candidate'; bg.save(invalidate)
        with self.assertRaisesRegex(RuntimeError, 'eligible'): bg.rollback(bg.release_id(images('blue')))

    def test_modified_container_image_cannot_be_switched(self):
        with patch.object(bg, 'slot_images', return_value=images('unexpected')), patch.object(bg, 'route') as route:
            with self.assertRaisesRegex(RuntimeError, 'images differ'): bg.switch('blue')
        route.assert_not_called()

    def test_drain_timeout_preserves_inactive_without_resuming_old_code(self):
        with patch.object(bg, 'exists', return_value=True), \
             patch.object(bg, 'inspect', return_value={'State': {'Status': 'running'}}), \
             patch.object(bg, 'control', return_value={**details(), 'activeJobs': 1}) as control:
            with self.assertRaisesRegex(RuntimeError, 'still has work'): bg.drain('blue', timeout=0)
        self.assertEqual([c.args for c in control.call_args_list], [('blue', 'drain'), ('blue',)])

    def test_retire_rechecks_work_admitted_during_connection_grace(self):
        calls = [details(), {**details(), 'sseSubscribers': 1}, details(), {**details(), 'activeJobs': 1}]
        with patch.object(bg, 'exists', return_value=True), \
             patch.object(bg, 'inspect', return_value={'State': {'Status': 'running'}}), \
             patch.object(bg, 'control', side_effect=calls) as control:
            with self.assertRaisesRegex(RuntimeError, 'not quiescent'):
                bg.drain('blue', timeout=0, connection_grace=0)
        self.assertIn(('blue', 'retire'), [c.args for c in control.call_args_list])

    def test_environment_reuses_active_values_and_applies_only_explicit_override(self):
        bg.atomic(bg.HOME / 'runtime/green/agent.env', 'API_KEY=deployed\n')
        candidate = bg.HOME / 'candidate.env'; bg.atomic(candidate, 'API_KEY=reviewed\n')
        snapshot = {'environment': {'API_KEY': 'obsolete'}}
        self.assertEqual(bg.env_values('agent', snapshot, 'green'), {'API_KEY': 'deployed'})
        self.assertEqual(bg.env_values('agent', snapshot, 'green', candidate), {'API_KEY': 'reviewed'})

    def test_environment_rejects_world_readable_files_and_symlinks(self):
        candidate = bg.HOME / 'candidate.env'; bg.atomic(candidate, 'API_KEY=value\n', 0o644)
        with self.assertRaisesRegex(RuntimeError, 'private'): bg.read_env(candidate)
        candidate.chmod(0o600)
        link = bg.HOME / 'linked.env'; link.symlink_to(candidate)
        with self.assertRaisesRegex(RuntimeError, 'private'): bg.read_env(link)

class AssetArchiveTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for attribute, value in [('HOME', self.root), ('ASSETS', self.root / 'assets')]:
            p = patch.object(bg, attribute, value); p.start(); self.addCleanup(p.stop)

    def test_assets_survive_slot_replacement_and_url_collisions_do_not_overwrite(self):
        content = [b'old release']
        def docker(*args): (Path(args[-1]) / 'app-hashed.wasm').write_bytes(content[0])
        with patch.object(bg, 'exists', return_value=True), patch.object(bg, 'docker', side_effect=docker):
            bg.archive_frontend('blue')
            bg.archive_frontend('green')
            content[0] = b'different contents at same URL'
            with self.assertRaisesRegex(RuntimeError, 'reused'): bg.archive_frontend('blue')
        self.assertEqual((bg.ASSETS / 'app-hashed.wasm').read_bytes(), b'old release')
        self.assertEqual(list((self.root / 'staging').iterdir()), [])

    def test_asset_symlinks_are_not_archived(self):
        def docker(*args): (Path(args[-1]) / 'private').symlink_to('/etc/passwd')
        with patch.object(bg, 'exists', return_value=True), patch.object(bg, 'docker', side_effect=docker):
            with self.assertRaisesRegex(RuntimeError, 'regular files'): bg.archive_frontend('blue')

    def test_public_asset_directories_are_readable_with_private_workflow_umask(self):
        def docker(*args):
            nested = Path(args[-1]) / 'fonts'; nested.mkdir()
            (nested / 'font-hash.woff2').write_bytes(b'font')
        previous = os.umask(0o077)
        try:
            with patch.object(bg, 'exists', return_value=True), patch.object(bg, 'docker', side_effect=docker):
                bg.archive_frontend('blue')
        finally:
            os.umask(previous)
        self.assertEqual(bg.ASSETS.stat().st_mode & 0o777, 0o755)
        self.assertEqual((bg.ASSETS / 'fonts').stat().st_mode & 0o777, 0o755)
        self.assertEqual((bg.ASSETS / 'fonts/font-hash.woff2').stat().st_mode & 0o777, 0o644)

class NginxRenderingTest(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('installer', Path(__file__).with_name('install-nginx.py'))
        self.installer = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.installer)

    def test_upgrade_is_idempotent_preserves_tls_and_does_not_depend_on_other_slot(self):
        original = '''server {
    listen 11111 ssl;
    ssl_certificate /existing/fullchain.pem;
    modsecurity on;
    add_header X-Content-Type-Options nosniff;
    location /api/ {
        proxy_pass http://10.10.10.11:8080;
    }
    location / {
        proxy_pass http://10.10.10.10;
    }
}
'''
        rendered = self.installer.render_site(original)
        self.assertEqual(self.installer.render_site(rendered), rendered)
        self.assertIn('listen 11111 ssl;', rendered)
        self.assertIn('modsecurity on;', rendered)
        self.assertIn('location ^~ /assets/', rendered)
        self.assertIn('root /var/lib/ieum-bluegreen;', rendered)
        self.assertNotIn('ieum_frontend_previous', rendered)

    def test_main_worker_timeout_is_added_once_and_respects_existing_policy(self):
        original = 'events {}\nhttp {}\n'
        rendered = self.installer.render_main(original)
        self.assertEqual(rendered.count('worker_shutdown_timeout'), 1)
        self.assertEqual(self.installer.render_main(rendered), rendered)
        self.assertEqual(self.installer.render_main('worker_shutdown_timeout 300s;\n'), 'worker_shutdown_timeout 300s;\n')

    def test_custom_asset_security_configuration_is_not_silently_replaced(self):
        original = '''server {
    location /api/ { proxy_pass http://ieum_backend; }
    location /assets/ { deny all; }
    location / {
        proxy_pass http://ieum_frontend;
    }
}
'''
        with self.assertRaisesRegex(RuntimeError, 'custom asset'): self.installer.render_site(original)

    def test_known_original_asset_fallback_is_upgraded_without_duplicate_locations(self):
        original = ('server {\n    location /api/ { proxy_pass http://ieum_backend; }\n'
            + self.installer.LEGACY_ASSET_LOCATIONS
            + '    location / {\n        proxy_pass http://ieum_frontend;\n    }\n}\n')
        rendered = self.installer.render_site(original)
        self.assertNotIn('ieum_frontend_previous', rendered)
        self.assertEqual(rendered.count('location ^~ /assets/'), 1)

if __name__ == '__main__': unittest.main()
