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
        bg.atomic(bg.HOME / 'control/pending-recovery-not-before', '2026-10-20T00:00:00Z\n')
        bg.atomic(bg.HOME / 'control/backend-time-zone', 'UTC\n')
        self.set_route('green')
        bg.atomic(bg.HOME / 'snapshots/legacy.json', json.dumps({
            app: {'image': 'legacy-' + app, 'environment': {}} for app in bg.APPS}))
        for attribute, kwargs in [
            ('exists', {'return_value': False}), ('slot_images', {'side_effect': images}),
            ('backend_identity', {'side_effect': lambda c: {
                'id': ('b' if c == 'blue' else 'c') * 64,
                'image': images(c)['backend'], 'name': bg.name(c, 'backend'),
                'restartPolicy': {'Name': 'unless-stopped', 'MaximumRetryCount': 0}}}),
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
        self.assertEqual(bg.state()['previous'], 'blue')
        self.assertEqual(bg.state()['slots']['blue']['status'], 'retained')
        self.assertFalse((bg.HOME / 'transaction.json').exists())

    def test_failed_new_candidate_is_not_mistaken_for_retained_release(self):
        data = bg.state(); data['previous'] = None; data['slots']['blue']['status'] = 'candidate'; bg.save(data)
        with patch.object(bg, 'route', side_effect=self.set_route), \
             patch.object(bg, 'verify_ingress', side_effect=[RuntimeError('probe'), None]):
            with self.assertRaisesRegex(RuntimeError, 'probe'): bg.switch('blue')
        self.assertEqual(bg.state()['slots']['blue']['status'], 'failed')
        self.assertIsNone(bg.state()['previous'])

    def test_failed_retained_probe_can_be_retried_with_same_release(self):
        with patch.object(bg, 'route', side_effect=self.set_route), \
             patch.object(bg, 'verify_ingress', side_effect=[RuntimeError('temporary probe'), None]):
            with self.assertRaisesRegex(RuntimeError, 'temporary probe'): bg.rollback(bg.release_id(images('blue')))
        with patch.object(bg, 'route', side_effect=self.set_route):
            bg.rollback(bg.release_id(images('blue')))
        self.assertEqual(bg.state()['active'], 'blue')

    def isolate(self, transaction, color):
        transaction.setdefault('isolated', {})[color] = transaction['backends'][color]
        bg.write_transaction(transaction)

    def test_force_switch_isolates_unresponsive_active_before_resuming_healthy_target(self):
        calls = []
        def control(color, action=None):
            calls.append((color, action))
            if color == 'green': raise OSError('active backend down')
            return details()
        def isolate(transaction, color):
            calls.append((color, 'isolate')); self.isolate(transaction, color)
        with patch.object(bg, 'control', side_effect=control), \
             patch.object(bg, 'quarantine_backend', side_effect=isolate), \
             patch.object(bg, 'route', side_effect=self.set_route):
            bg.switch('blue', force=True)
        self.assertEqual(calls[:3], [('green', 'drain'), ('green', 'isolate'), ('blue', 'resume')])
        self.assertEqual(bg.state()['active'], 'blue')
        self.assertEqual(bg.state()['slots']['green']['status'], 'isolated')
        self.assertIsNone(bg.state()['previous'])
        self.assertFalse((bg.HOME / 'transaction.json').exists())
        self.assertEqual([c.args[0] for c in bg.verify_ingress.call_args_list], ['blue'])

    def test_unresponsive_source_is_not_silently_ignored_without_force(self):
        def control(color, action=None):
            if color == 'green': raise OSError('active backend down')
            return details()
        with patch.object(bg, 'control', side_effect=control), \
             patch.object(bg, 'quarantine_backend') as isolate, \
             patch.object(bg, 'route', side_effect=self.set_route):
            with self.assertRaisesRegex(RuntimeError, 'rollback is incomplete'): bg.switch('blue')
        isolate.assert_not_called()
        self.assertTrue((bg.HOME / 'transaction.json').exists())

    def test_force_recovery_can_finish_healthy_target_after_failed_source_drain(self):
        def control(color, action=None):
            if color == 'green': raise OSError('active backend down')
            return details()
        with patch.object(bg, 'control', side_effect=control), patch.object(bg, 'route', side_effect=self.set_route):
            with self.assertRaises(RuntimeError): bg.switch('blue')
            with patch.object(bg, 'quarantine_backend', side_effect=self.isolate):
                bg.recover(force=True, target='blue')
        self.assertEqual(bg.state()['active'], 'blue')
        self.assertFalse((bg.HOME / 'transaction.json').exists())

    def test_dead_candidate_cannot_prevent_previous_background_resuming(self):
        calls = []
        def control(color, action=None):
            calls.append((color, action))
            if (color, action) == ('blue', 'drain'): raise OSError('candidate died')
            return details()
        with patch.object(bg, 'control', side_effect=control), \
             patch.object(bg, 'route', side_effect=self.set_route), \
             patch.object(bg, 'verify_ingress', side_effect=[RuntimeError('candidate died'), None]), \
             patch.object(bg, 'quarantine_backend', side_effect=self.isolate) as isolate:
            with self.assertRaisesRegex(RuntimeError, 'candidate died'): bg.switch('blue')
        self.assertLess(calls.index(('green', 'resume')), calls.index(('blue', 'drain')))
        isolate.assert_called_once()
        self.assertEqual(bg.state()['active'], 'green')
        self.assertEqual(bg.state()['slots']['blue']['status'], 'isolated')
        self.assertFalse((bg.HOME / 'transaction.json').exists())

    def test_failed_candidate_isolation_preserves_journal_but_old_background_is_resumed(self):
        def control(color, action=None):
            if (color, action) == ('blue', 'drain'): raise OSError('candidate died')
            return details()
        with patch.object(bg, 'control', side_effect=control) as controls, \
             patch.object(bg, 'route', side_effect=self.set_route), \
             patch.object(bg, 'verify_ingress', side_effect=RuntimeError('candidate died')), \
             patch.object(bg, 'quarantine_backend', side_effect=RuntimeError('Docker unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'rollback is incomplete'): bg.switch('blue')
        self.assertIn(('green', 'resume'), [c.args for c in controls.call_args_list])
        self.assertTrue((bg.HOME / 'transaction.json').exists())

    def test_abandon_requires_force_and_archives_journal_after_isolating_both_backends(self):
        original = bg.state()
        transaction = {'from': 'green', 'to': 'blue', 'snapshot': original, 'targetImages': images('blue'),
            'backends': {c: bg.backend_identity(c) for c in ('blue', 'green')}}
        bg.write_transaction(transaction)
        with patch.object(bg, 'quarantine_backend', side_effect=self.isolate) as isolate:
            with self.assertRaisesRegex(RuntimeError, '--abandon --force'): bg.recover(abandon=True)
            isolate.assert_not_called()
            bg.recover(abandon=True, force=True)
        self.assertEqual([c.args[1] for c in isolate.call_args_list], ['green', 'blue'])
        self.assertEqual(bg.state()['active'], 'green')
        self.assertIsNone(bg.state()['previous'])
        self.assertFalse((bg.HOME / 'transaction.json').exists())
        self.assertEqual(len(list((bg.HOME / 'abandoned-transactions').glob('*.json'))), 1)

    def test_environment_replace_removes_omitted_keys_without_changing_other_apps(self):
        bg.atomic(bg.HOME / 'runtime/green/backend.env', 'KEEP=old\nDELETE=old\n')
        candidate = bg.HOME / 'candidate.env'; bg.atomic(candidate, 'KEEP=new\n')
        self.assertEqual(bg.env_values('backend', {}, 'green', candidate), {'KEEP': 'new', 'DELETE': 'old'})
        self.assertEqual(bg.env_values('backend', {}, 'green', candidate, replace=True), {'KEEP': 'new'})
        self.assertEqual(bg.env_values('backend', {}, 'green'), {'KEEP': 'old', 'DELETE': 'old'})

    def test_pending_cutoff_requires_explicit_offset_and_is_immutable(self):
        (bg.HOME / 'control/pending-recovery-not-before').unlink()
        with self.assertRaisesRegex(RuntimeError, 'Review existing PENDING'): bg.pending_recovery_cutoff()
        with self.assertRaisesRegex(RuntimeError, 'ISO-8601'): bg.pending_recovery_cutoff('2026-10-20T09:00:00')
        self.assertEqual(bg.pending_recovery_cutoff('2026-10-20T09:00:00+09:00'), '2026-10-20T00:00:00Z')
        self.assertEqual(bg.pending_recovery_cutoff(), '2026-10-20T00:00:00Z')
        with self.assertRaisesRegex(RuntimeError, 'immutable'): bg.pending_recovery_cutoff('2026-10-21T00:00:00Z')

    def test_backend_zone_requires_operator_review_and_is_immutable(self):
        path = bg.HOME / 'control/backend-time-zone'; path.unlink()
        with self.assertRaisesRegex(RuntimeError, 'Confirm the Legacy JVM'): bg.backend_time_zone()
        for invalid in ('Asia/seoul', '+09:00', '../UTC', ' UTC', ''):
            with self.assertRaisesRegex(RuntimeError, 'IANA zone'): bg.normalized_backend_zone(invalid)
        self.assertEqual(bg.backend_time_zone('Asia/Seoul'), 'Asia/Seoul')
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)
        self.assertEqual(bg.backend_time_zone(), 'Asia/Seoul')
        with self.assertRaisesRegex(RuntimeError, 'immutable'): bg.backend_time_zone('UTC')
        self.assertEqual(path.read_text(), 'Asia/Seoul\n')

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
            'from': 'green', 'to': 'blue', 'snapshot': original, 'targetImages': images('blue'),
            'backends': {c: bg.backend_identity(c) for c in ('blue', 'green')}}))
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

    def test_force_switch_uses_snapshot_when_isolated_source_disappears(self):
        vanished = set()
        def current_images(color):
            if color in vanished: raise RuntimeError('No such container')
            return images(color)
        def isolate(transaction, color):
            self.isolate(transaction, color); vanished.add(color)
        def control(color, action=None):
            if (color, action) == ('green', 'drain'): raise OSError('source disappeared')
            return details()
        with patch.object(bg, 'slot_images', side_effect=current_images), \
             patch.object(bg, 'control', side_effect=control), \
             patch.object(bg, 'quarantine_backend', side_effect=isolate), \
             patch.object(bg, 'route', side_effect=self.set_route):
            bg.switch('blue', force=True)
        self.assertEqual(bg.state()['active'], 'blue')
        self.assertEqual(bg.state()['slots']['green']['images'], images('green'))
        self.assertIsNone(bg.state()['previous'])
        self.assertFalse((bg.HOME / 'transaction.json').exists())

    def test_force_recover_uses_snapshot_for_absent_isolated_source(self):
        transaction = {'from': 'green', 'to': 'blue', 'snapshot': bg.state(),
            'targetImages': images('blue'), 'backends': {c: bg.backend_identity(c) for c in ('blue', 'green')}}
        self.isolate(transaction, 'green')
        def current_images(color):
            if color == 'green': raise RuntimeError('No such container')
            return images(color)
        with patch.object(bg, 'slot_images', side_effect=current_images), \
             patch.object(bg, 'route', side_effect=self.set_route):
            bg.recover(force=True, target='blue')
        self.assertEqual(bg.state()['active'], 'blue')
        self.assertEqual(bg.state()['slots']['green']['status'], 'isolated')
        self.assertFalse((bg.HOME / 'transaction.json').exists())

    def test_recovery_refuses_same_image_replacement_backend_before_resuming(self):
        identities = {c: bg.backend_identity(c) for c in ('blue', 'green')}
        transaction = {'from': 'green', 'to': 'blue', 'snapshot': bg.state(),
            'targetImages': images('blue'), 'backends': identities}
        bg.write_transaction(transaction)
        def replacement(color):
            return dict(identities[color], id='d' * 64)
        for target in ('blue', 'green'):
            with patch.object(bg, 'backend_identity', side_effect=replacement), patch.object(bg, 'route') as route:
                with self.assertRaisesRegex(RuntimeError, 'identity changed'):
                    bg.recover(force=True, target=target)
            route.assert_not_called()
        bg.control.assert_not_called()
        self.assertTrue((bg.HOME / 'transaction.json').exists())

    def test_isolated_backend_restart_policy_restored_only_after_healthy_recovery(self):
        for target in ('blue', 'green'):
            transaction = {'from': 'green', 'to': 'blue', 'snapshot': bg.state(),
                'targetImages': images('blue'), 'backends': {c: bg.backend_identity(c) for c in ('blue', 'green')}}
            self.isolate(transaction, target)
            def restored(tx, color):
                bg.verify_ingress.assert_called_with(target)
                self.assertIn(target, tx['isolated'])
            with patch.object(bg, 'restore_restart_policy', side_effect=restored) as restore, \
                 patch.object(bg, 'route', side_effect=self.set_route):
                bg.recover(force=True, target=target)
            restore.assert_called_once()
            self.assertFalse((bg.HOME / 'transaction.json').exists())

    def test_failed_restart_policy_restore_preserves_isolation_and_journal(self):
        transaction = {'from': 'green', 'to': 'blue', 'snapshot': bg.state(),
            'targetImages': images('blue'), 'backends': {c: bg.backend_identity(c) for c in ('blue', 'green')}}
        self.isolate(transaction, 'blue')
        with patch.object(bg, 'restore_restart_policy', side_effect=RuntimeError('Docker unavailable')), \
             patch.object(bg, 'route', side_effect=self.set_route):
            with self.assertRaisesRegex(RuntimeError, 'Docker unavailable'): bg.recover(force=True, target='blue')
        saved = json.loads((bg.HOME / 'transaction.json').read_text())
        self.assertIn('blue', saved['isolated'])

    def test_force_inactive_replacement_quarantines_before_removal(self):
        calls = []
        def isolate(transaction, color):
            self.isolate(transaction, color); calls.append('isolated')
        def docker(*args):
            self.assertIn('isolated', calls)
            self.assertTrue((bg.HOME / 'transaction.json').exists())
            calls.append(args[0])
        with patch.object(bg, 'exists', side_effect=lambda n: n.startswith('ieum-blue-')), \
             patch.object(bg, 'inspect', return_value={'Config': {'Labels': {'ieum.bluegreen.managed': 'true'}},
                 'State': {'Status': 'exited'}}), \
             patch.object(bg, 'drain', side_effect=OSError('unresponsive inactive')), \
             patch.object(bg, 'quarantine_backend', side_effect=isolate), \
             patch.object(bg, 'docker', side_effect=docker):
            bg.remove_inactive('blue', force=True)
        self.assertEqual(calls, ['isolated', 'rm', 'rm', 'rm'])
        self.assertFalse((bg.HOME / 'transaction.json').exists())
        self.assertEqual(bg.state()['slots']['blue']['status'], 'preparing')

    def test_failed_inactive_isolation_keeps_journal_and_never_removes_containers(self):
        with patch.object(bg, 'drain', side_effect=OSError('unresponsive inactive')), \
             patch.object(bg, 'quarantine_backend', side_effect=RuntimeError('isolation failed')), \
             patch.object(bg, 'docker') as docker:
            with self.assertRaisesRegex(RuntimeError, 'isolation failed'):
                bg.remove_inactive('blue', force=True)
        docker.assert_not_called()
        self.assertTrue((bg.HOME / 'transaction.json').exists())

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

    def legacy_fixture(self):
        data = bg.state(); data['active'] = 'legacy'; data['previous'] = None; bg.save(data)
        self.set_route('legacy'); bg.atomic(bg.HOME / 'control/active', 'legacy\n')
        bg.atomic(bg.HOME / 'control/phase', 'legacy\n')
        infos = {bg.LEGACY[app]: {'Id': format(index + 1, '064x'), 'Image': 'current-' + app,
            'State': {'Status': 'running'}, 'Config': {'Env': ['BASE=default', 'CURRENT=' + app,
                'SPRING_PROFILES_ACTIVE=dev']}} for index, app in enumerate(bg.APPS)}
        created = {}
        def docker(*args):
            if args[:2] == ('image', 'inspect'):
                return json.dumps([{'Id': args[2], 'Config': {'Env': ['BASE=default'],
                    'Labels': {'ieum.bluegreen.capable': 'true', 'ieum.bluegreen.protocol': '2'}}}])
            if args[0] == 'create':
                created[args[args.index('--name') + 1]] = {
                    'image': args[-1], 'env': bg.read_env(args[args.index('--env-file') + 1])}
            return ''
        return infos, created, docker

    def test_legacy_preparation_uses_live_images_and_env_instead_of_init_snapshot(self):
        infos, created, docker = self.legacy_fixture()
        original_snapshot = (bg.HOME / 'snapshots/legacy.json').read_bytes()
        with patch.object(bg, 'inspect', side_effect=lambda n: infos[n]), \
             patch.object(bg, 'docker', side_effect=docker), \
             patch.object(bg, 'remove_inactive', side_effect=bg.invalidate_slot):
            bg.prepare('blue', {'backend': 'new-protocol2-backend'})
        for app in bg.APPS:
            candidate = created[bg.name('blue', app)]
            self.assertEqual(candidate['image'], 'new-protocol2-backend' if app == 'backend' else 'current-' + app)
            self.assertEqual(candidate['env']['CURRENT'], app)
            self.assertEqual(candidate['env']['SPRING_PROFILES_ACTIVE'], 'dev')
            self.assertNotIn('BASE', candidate['env'])
        self.assertEqual((bg.HOME / 'snapshots/legacy.json').read_bytes(), original_snapshot)

    def test_legacy_preparation_replaces_only_explicit_app_environment(self):
        infos, created, docker = self.legacy_fixture()
        candidate = bg.HOME / 'backend-candidate.env'; bg.atomic(candidate, 'REVIEWED=new\n')
        with patch.object(bg, 'inspect', side_effect=lambda n: infos[n]), \
             patch.object(bg, 'docker', side_effect=docker), \
             patch.object(bg, 'remove_inactive', side_effect=bg.invalidate_slot):
            bg.prepare('blue', {'backend': 'new-backend'}, {'backend': candidate}, replace_env=True)
        self.assertEqual(created[bg.name('blue', 'backend')]['env']['REVIEWED'], 'new')
        self.assertNotIn('CURRENT', created[bg.name('blue', 'backend')]['env'])
        self.assertEqual(created[bg.name('blue', 'backend')]['env']['TZ'], 'UTC')
        self.assertEqual(created[bg.name('blue', 'backend')]['env'][
            'IEUM_WORKFLOW_QUEUE_PENDING_RECOVERY_EXPECTED_ZONE'], 'UTC')
        self.assertEqual(created[bg.name('blue', 'agent')]['env']['CURRENT'], 'agent')

    def test_concurrent_legacy_redeployment_aborts_before_inactive_removal(self):
        infos, created, docker = self.legacy_fixture()
        count = 0
        def inspect(container):
            nonlocal count
            count += 1
            result = json.loads(json.dumps(infos[container]))
            if count > len(bg.APPS): result['Id'] = 'f' * 64
            return result
        with patch.object(bg, 'inspect', side_effect=inspect), patch.object(bg, 'docker', side_effect=docker), \
             patch.object(bg, 'remove_inactive') as remove:
            with self.assertRaisesRegex(RuntimeError, 'Legacy source changed'):
                bg.prepare('blue', {'backend': 'new-backend'})
        remove.assert_not_called(); self.assertFalse(created)

    def test_legacy_changed_during_candidate_creation_is_cleaned_up_and_not_eligible(self):
        infos, created, docker = self.legacy_fixture()
        def create_then_change(*args):
            result = docker(*args)
            if args[0] == 'create': infos[bg.LEGACY['frontend']]['Id'] = 'f' * 64
            return result
        with patch.object(bg, 'inspect', side_effect=lambda n: infos[n]), \
             patch.object(bg, 'docker', side_effect=create_then_change) as calls, \
             patch.object(bg, 'remove_inactive', side_effect=bg.invalidate_slot):
            with self.assertRaisesRegex(RuntimeError, 'Legacy source changed'):
                bg.prepare('blue', {'backend': 'new-backend'})
        self.assertEqual(bg.state()['slots']['blue']['status'], 'failed')
        self.assertEqual(bg.state()['active'], 'legacy')
        self.assertEqual(len([c for c in calls.call_args_list if c.args[0] == 'rm']), len(bg.APPS))

    def test_legacy_new_env_cutoff_can_be_supplied_through_prepare(self):
        infos, created, docker = self.legacy_fixture()
        (bg.HOME / 'control/pending-recovery-not-before').unlink()
        candidate = bg.HOME / 'backend-candidate.env'
        bg.atomic(candidate, 'IEUM_WORKFLOW_QUEUE_PENDING_RECOVERY_NOT_BEFORE=2026-10-21T09:00:00+09:00\n')
        with patch.object(bg, 'inspect', side_effect=lambda n: infos[n]), \
             patch.object(bg, 'docker', side_effect=docker), \
             patch.object(bg, 'remove_inactive', side_effect=bg.invalidate_slot):
            bg.prepare('blue', {'backend': 'new-backend'}, {'backend': candidate})
        self.assertEqual(created[bg.name('blue', 'backend')]['env'][
            'IEUM_WORKFLOW_QUEUE_PENDING_RECOVERY_NOT_BEFORE'], '2026-10-21T00:00:00Z')

    def test_stopped_or_unidentifiable_legacy_never_uses_historical_fallback(self):
        for change in ({'State': {'Status': 'exited'}}, {'Id': ''}):
            infos, created, docker = self.legacy_fixture()
            infos[bg.LEGACY['frontend']].update(change)
            with patch.object(bg, 'inspect', side_effect=lambda n: infos[n]), \
                 patch.object(bg, 'docker', side_effect=docker), patch.object(bg, 'remove_inactive') as remove:
                with self.assertRaisesRegex(RuntimeError, 'Legacy source must be running'):
                    bg.prepare('blue', {'backend': 'new-backend'})
            remove.assert_not_called(); self.assertFalse(created)

    def test_prepare_cli_accepts_service_env_and_replace_with_bounded_lock(self):
        with patch.object(bg.sys, 'argv', ['bluegreen', '--lock-timeout', '0', 'prepare', 'blue',
             '--backend-image', 'reviewed-image', '--backend-env-file', '/private/backend.env', '--replace-env',
             '--backend-time-zone', 'UTC']), \
             patch.object(bg, 'prepare') as prepare:
            bg.main()
        prepare.assert_called_once_with('blue', {'backend': 'reviewed-image'},
            {'backend': '/private/backend.env'}, replace_env=True, recovery_not_before=None, force=False,
            backend_zone='UTC')

    def test_legacy_runtime_tz_alone_does_not_count_as_operator_confirmation(self):
        infos, created, docker = self.legacy_fixture()
        (bg.HOME / 'control/backend-time-zone').unlink()
        infos[bg.LEGACY['backend']]['Config']['Env'].append('TZ=UTC')
        with patch.object(bg, 'inspect', side_effect=lambda n: infos[n]), \
             patch.object(bg, 'docker', side_effect=docker), patch.object(bg, 'remove_inactive') as remove:
            with self.assertRaisesRegex(RuntimeError, 'Confirm the Legacy JVM'):
                bg.prepare('blue', {'backend': 'reviewed-image'})
        remove.assert_not_called(); self.assertFalse(created)

    def test_reviewed_zone_from_backend_file_is_pinned_and_preserves_jvm_options(self):
        infos, created, docker = self.legacy_fixture()
        (bg.HOME / 'control/backend-time-zone').unlink()
        candidate = bg.HOME / 'backend-candidate.env'
        bg.atomic(candidate, 'IEUM_WORKFLOW_QUEUE_PENDING_RECOVERY_EXPECTED_ZONE=Asia/Seoul\n'
            'JAVA_TOOL_OPTIONS=-Xmx768m -Duser.timezone=Asia/Seoul\n')
        with patch.object(bg, 'inspect', side_effect=lambda n: infos[n]), \
             patch.object(bg, 'docker', side_effect=docker), \
             patch.object(bg, 'remove_inactive', side_effect=bg.invalidate_slot):
            bg.prepare('blue', {'backend': 'reviewed-image'}, {'backend': candidate}, replace_env=True)
        env = created[bg.name('blue', 'backend')]['env']
        self.assertEqual(env['TZ'], 'Asia/Seoul')
        self.assertEqual(env['IEUM_WORKFLOW_QUEUE_PENDING_RECOVERY_EXPECTED_ZONE'], 'Asia/Seoul')
        self.assertEqual(env['JAVA_TOOL_OPTIONS'], '-Xmx768m -Duser.timezone=Asia/Seoul')
        self.assertEqual(bg.backend_time_zone(), 'Asia/Seoul')

    def test_conflicting_zone_policies_fail_before_replacing_candidate(self):
        for cli, configured in (('Asia/Seoul', 'UTC'), (None, 'Asia/Seoul')):
            infos, created, docker = self.legacy_fixture()
            candidate = bg.HOME / 'backend-candidate.env'
            bg.atomic(candidate, 'IEUM_WORKFLOW_QUEUE_PENDING_RECOVERY_EXPECTED_ZONE=' + configured + '\n')
            with patch.object(bg, 'inspect', side_effect=lambda n: infos[n]), \
                 patch.object(bg, 'docker', side_effect=docker), patch.object(bg, 'remove_inactive') as remove:
                with self.assertRaisesRegex(RuntimeError, 'different expected time zones|immutable'):
                    bg.prepare('blue', {'backend': 'reviewed-image'}, {'backend': candidate}, backend_zone=cli)
            remove.assert_not_called(); self.assertFalse(created)

    def test_force_explicitly_allows_interrupting_busy_retained_environment(self):
        with patch.object(bg, 'drain', side_effect=RuntimeError('Previous environment still has work')), \
             patch.object(bg, 'quarantine_backend', side_effect=self.isolate) as isolate:
            with self.assertRaisesRegex(RuntimeError, 'still has work'): bg.remove_inactive('blue')
            isolate.assert_not_called()
            bg.remove_inactive('blue', force=True)
        isolate.assert_called_once()

class DeploymentLockTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        p = patch.object(bg, 'HOME', self.root); p.start(); self.addCleanup(p.stop)

    def test_busy_lock_times_out_without_entering_or_removing_owners_record(self):
        with bg.deployment_lock(0, 'deploy'):
            owner = (bg.HOME / 'lock-owner.json').read_bytes()
            inode = (bg.HOME / 'deploy.lock').stat().st_ino
            with self.assertRaisesRegex(RuntimeError, 'operation was not started'):
                with bg.deployment_lock(0.02, 'rollback'): self.fail('Busy lock entered')
            self.assertEqual((bg.HOME / 'lock-owner.json').read_bytes(), owner)
        self.assertFalse((bg.HOME / 'lock-owner.json').exists())
        with bg.deployment_lock(0, 'recover'):
            self.assertEqual((bg.HOME / 'deploy.lock').stat().st_ino, inode)
            self.assertEqual(json.loads((bg.HOME / 'lock-owner.json').read_text())['command'], 'recover')

    def test_exception_releases_lock_and_cleans_advisory_record(self):
        with self.assertRaisesRegex(RuntimeError, 'operation failed'):
            with bg.deployment_lock(0, 'prepare'): raise RuntimeError('operation failed')
        self.assertFalse((bg.HOME / 'lock-owner.json').exists())
        with bg.deployment_lock(0, 'recover'): pass

    def test_timeout_rejects_negative_nonfinite_and_unbounded_values(self):
        for value in ('-1', 'NaN', 'inf', str(bg.MAX_LOCK_TIMEOUT + 1), 'invalid'):
            with self.assertRaises(bg.argparse.ArgumentTypeError): bg.lock_timeout(value)
        self.assertEqual(bg.lock_timeout('0'), 0)
        self.assertEqual(bg.lock_timeout('30'), 30)

    def test_default_deploy_wait_survives_a_700_second_lock_holder_while_emergency_is_bounded(self):
        elapsed = [0.0]
        def flock(*args):
            if elapsed[0] < 700: raise BlockingIOError('held by another repository')
        def sleep(seconds): elapsed[0] += seconds
        with patch.object(bg.time, 'monotonic', side_effect=lambda: elapsed[0]), \
             patch.object(bg.time, 'sleep', side_effect=sleep), patch.object(bg.fcntl, 'flock', side_effect=flock):
            with bg.deployment_lock(bg.default_lock_timeout('deploy'), 'deploy'):
                self.assertGreaterEqual(elapsed[0], 700)
            elapsed[0] = 0
            with self.assertRaisesRegex(RuntimeError, 'operation was not started'):
                with bg.deployment_lock(30, 'rollback'): self.fail('Emergency operation acquired busy lock')
        self.assertEqual(elapsed[0], 30)

    def test_deploy_default_is_separate_from_operator_wait_and_covers_prior_normal_deployments(self):
        # Three repositories can overlap. Account for two prior normal releases' bounded phases;
        # Docker I/O and arbitrary queue depth remain finite-wait operational limits.
        normal_phases = 2 * bg.JOB_DRAIN_TIMEOUT + bg.CONNECTION_GRACE + \
            len(bg.APPS) * bg.STOP_TIMEOUT + 2 * 120 + 2 * 15 + 4 * 30
        self.assertGreater(bg.default_lock_timeout('deploy'), 2 * normal_phases)
        self.assertEqual(bg.default_lock_timeout('recover'), 600)
        self.assertEqual(bg.default_lock_timeout('deploy'), 14400)

class BackendIsolationTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        p = patch.object(bg, 'HOME', self.root); p.start(); self.addCleanup(p.stop)
        self.expected = {'id': 'b' * 64, 'image': 'sha256:reviewed-backend', 'name': 'ieum-blue-backend'}
        self.transaction = {'from': 'green', 'to': 'blue', 'backends': {'blue': self.expected}}
        self.info = {'Id': self.expected['id'], 'Image': self.expected['image'],
            'Config': {'Labels': {'ieum.bluegreen.managed': 'true', 'ieum.bluegreen.color': 'blue'}},
            'State': {'Status': 'running', 'Running': True},
            'HostConfig': {'RestartPolicy': {'Name': 'unless-stopped'}}}

    def docker(self, *args):
        if args[0] == 'ps': return self.expected['id'] + ' ' + self.expected['name']
        if args[0] == 'update': self.info['HostConfig']['RestartPolicy']['Name'] = 'no'
        if args[0] == 'stop': self.info['State'] = {'Status': 'exited', 'Running': False}
        return ''

    def test_isolation_stops_only_pinned_id_and_verifies_restart_is_disabled(self):
        with patch.object(bg, 'docker', side_effect=self.docker) as docker, \
             patch.object(bg, 'inspect', side_effect=lambda n: json.loads(json.dumps(self.info))):
            bg.quarantine_backend(self.transaction, 'blue')
        self.assertIn(('update', '--restart=no', self.expected['id']), [c.args for c in docker.call_args_list])
        self.assertIn(('stop', '--timeout', '30', self.expected['id']), [c.args for c in docker.call_args_list])
        self.assertEqual(self.transaction['isolated']['blue'], self.expected)

    def test_replacement_container_is_never_stopped(self):
        self.info['Id'] = 'c' * 64
        with patch.object(bg, 'docker', side_effect=self.docker) as docker, \
             patch.object(bg, 'inspect', return_value=self.info):
            with self.assertRaisesRegex(RuntimeError, 'container changed'):
                bg.quarantine_backend(self.transaction, 'blue')
        self.assertEqual([c.args[0] for c in docker.call_args_list], ['ps'])
        self.assertNotIn('isolated', self.transaction)

    def test_unavailable_docker_is_not_mistaken_for_absent_worker(self):
        with patch.object(bg, 'docker', side_effect=RuntimeError('Docker daemon unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'Docker daemon unavailable'):
                bg.quarantine_backend(self.transaction, 'blue')
        self.assertNotIn('isolated', self.transaction)

    def test_container_must_really_stop_before_isolation_is_recorded(self):
        def docker(*args):
            if args[0] == 'ps': return self.expected['id'] + ' ' + self.expected['name']
            if args[0] == 'update': self.info['HostConfig']['RestartPolicy']['Name'] = 'no'
            return ''
        with patch.object(bg, 'docker', side_effect=docker), patch.object(bg, 'inspect', return_value=self.info):
            with self.assertRaisesRegex(RuntimeError, 'isolation could not be verified'):
                bg.quarantine_backend(self.transaction, 'blue')
        self.assertNotIn('isolated', self.transaction)
        self.assertTrue((bg.HOME / 'transaction.json').exists())

    def test_authoritatively_absent_original_is_isolated_without_stopping_other_containers(self):
        with patch.object(bg, 'docker', return_value='') as docker, patch.object(bg, 'inspect') as inspect:
            bg.quarantine_backend(self.transaction, 'blue')
        self.assertEqual(docker.call_count, 1)
        inspect.assert_not_called()
        self.assertEqual(self.transaction['isolated']['blue'], self.expected)

    def test_renamed_original_requires_manual_inspection(self):
        with patch.object(bg, 'docker', return_value=self.expected['id'] + ' renamed-backend') as docker:
            with self.assertRaisesRegex(RuntimeError, 'was renamed'):
                bg.quarantine_backend(self.transaction, 'blue')
        self.assertEqual(docker.call_count, 1)
        self.assertNotIn('isolated', self.transaction)

    def test_original_restart_policy_is_restored_by_exact_id_and_verified(self):
        self.expected['restartPolicy'] = {'Name': 'on-failure', 'MaximumRetryCount': 5}
        current = dict(self.expected, restartPolicy={'Name': 'no', 'MaximumRetryCount': 0})
        def docker(*args):
            self.assertEqual(args, ('update', '--restart=on-failure:5', self.expected['id']))
            current['restartPolicy'] = dict(self.expected['restartPolicy'])
        with patch.object(bg, 'backend_identity', side_effect=lambda c: dict(current)), \
             patch.object(bg, 'docker', side_effect=docker):
            bg.restore_restart_policy(self.transaction, 'blue')

    def test_restart_policy_restore_never_updates_replacement_or_unverified_policy(self):
        self.expected['restartPolicy'] = {'Name': 'unless-stopped', 'MaximumRetryCount': 0}
        for actual in (dict(self.expected, id='c' * 64), self.expected):
            with patch.object(bg, 'backend_identity', return_value=actual), patch.object(bg, 'docker') as docker:
                if actual['id'] != self.expected['id']:
                    with self.assertRaisesRegex(RuntimeError, 'identity changed'):
                        bg.restore_restart_policy(self.transaction, 'blue')
                    docker.assert_not_called()
                else:
                    actual['restartPolicy'] = {'Name': 'no', 'MaximumRetryCount': 0}
                    # The pinned policy must not be overwritten by mutable inspect data.
                    self.transaction['backends']['blue'] = dict(self.expected,
                        restartPolicy={'Name': 'unless-stopped', 'MaximumRetryCount': 0})
                    with self.assertRaisesRegex(RuntimeError, 'could not be verified'):
                        bg.restore_restart_policy(self.transaction, 'blue')

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
