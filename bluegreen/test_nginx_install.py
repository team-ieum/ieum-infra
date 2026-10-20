import fcntl
import importlib.util
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location('nginx_installer', Path(__file__).with_name('install-nginx.py'))
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallationLockTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.owner = SimpleNamespace(pw_uid=os.geteuid(), pw_gid=os.getegid())
        replacement = patch.object(installer, 'DEPLOYMENT_HOME', self.home)
        replacement.start()
        self.addCleanup(replacement.stop)

    def assert_other_deployment_is_blocked(self):
        with (self.home / 'deploy.lock').open('a') as deployment:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(deployment, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_new_lock_is_owned_by_the_deployment_account_and_never_unlinked(self):
        with patch.object(installer.os, 'fchown', wraps=os.fchown) as chown:
            with installer.deployment_lock(self.owner):
                metadata = (self.home / 'deploy.lock').stat()
                self.assertEqual(metadata.st_uid, self.owner.pw_uid)
                self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o600)
                self.assert_other_deployment_is_blocked()
            chown.assert_called_once()
        self.assertTrue((self.home / 'deploy.lock').exists())
        with (self.home / 'deploy.lock').open('a') as deployment:
            fcntl.flock(deployment, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_existing_lock_inode_and_owner_are_preserved(self):
        path = self.home / 'deploy.lock'
        path.touch(mode=0o644)
        before = path.stat()
        with patch.object(installer.os, 'fchown') as chown:
            with installer.deployment_lock(self.owner):
                self.assertEqual(path.stat().st_ino, before.st_ino)
                self.assert_other_deployment_is_blocked()
            chown.assert_not_called()
        self.assertEqual(path.stat().st_mode, before.st_mode)

    def test_lock_is_held_until_installation_failure_and_rollback_have_finished(self):
        def fail_after_simulated_rollback(owner):
            self.assert_other_deployment_is_blocked()
            # The installation routine also handles its rollback before raising.
            self.assert_other_deployment_is_blocked()
            raise RuntimeError('installation and rollback completed')
        with patch.object(installer.os, 'geteuid', return_value=0), \
             patch.object(installer.pwd, 'getpwnam', return_value=self.owner), \
             patch.object(installer, 'install_locked', side_effect=fail_after_simulated_rollback):
            with self.assertRaisesRegex(RuntimeError, 'rollback completed'):
                installer.main()
        with (self.home / 'deploy.lock').open('a') as deployment:
            fcntl.flock(deployment, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_missing_deployment_home_is_not_created_by_root(self):
        absent = self.home / 'not-created'
        with patch.object(installer, 'DEPLOYMENT_HOME', absent):
            with self.assertRaisesRegex(RuntimeError, 'as kwung'):
                with installer.deployment_lock(self.owner):
                    self.fail('The installation must not start')
        self.assertFalse(absent.exists())

    def test_symlink_lock_is_rejected_without_changing_its_target(self):
        target = self.home / 'do-not-touch'
        target.write_text('preserved')
        (self.home / 'deploy.lock').symlink_to(target)
        with self.assertRaises(OSError):
            with installer.deployment_lock(self.owner):
                self.fail('A symlink cannot coordinate deployments safely')
        self.assertEqual(target.read_text(), 'preserved')

    def test_unexpected_lock_owner_is_rejected(self):
        (self.home / 'deploy.lock').touch()
        wrong_owner = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=self.owner.pw_uid + 1)
        with patch.object(installer.os, 'fstat', return_value=wrong_owner):
            with self.assertRaisesRegex(RuntimeError, 'owned'):
                with installer.deployment_lock(self.owner):
                    self.fail('Do not take a root-owned deployment lock')


class ArchiveHeaderPolicyTest(unittest.TestCase):
    def original_site(self):
        return '''server {
    listen 11111 ssl;
    ssl_certificate /existing/fullchain.pem;
    add_header X-Content-Type-Options nosniff always;
    add_header Content-Security-Policy "default-src 'self'" always;
    add_header X-Future-Security-Policy "future-value" always;
    location /api/ {
        proxy_pass http://10.10.10.11:8080;
    }
    location / {
        proxy_pass http://10.10.10.10;
    }
}
'''

    def test_archive_inherits_the_existing_and_future_security_header_policy(self):
        original = self.original_site()
        rendered = installer.render_site(original)
        for line in original.splitlines():
            if 'add_header ' in line:
                self.assertIn(line, rendered)
        archive = rendered.split('location @ieum_archived_assets {', 1)[1].split('}', 1)[0]
        self.assertNotIn('add_header', archive)
        self.assertIn('expires 30d;', archive)
        self.assertEqual(installer.render_site(rendered), rendered)

    def test_both_existing_managed_asset_configurations_upgrade_idempotently(self):
        for existing in (installer.LEGACY_ASSET_LOCATIONS, installer.PREVIOUS_ARCHIVE_ASSET_LOCATIONS):
            with self.subTest(existing=existing):
                original = self.original_site().replace('    location / {', existing + '    location / {')
                rendered = installer.render_site(original)
                self.assertEqual(rendered.count('location @ieum_archived_assets'), 1)
                self.assertNotIn('location @ieum_previous_assets', rendered)
                self.assertNotIn('add_header Cache-Control', rendered)
                self.assertIn('add_header X-Future-Security-Policy "future-value" always;', rendered)
                self.assertEqual(installer.render_site(rendered), rendered)


if __name__ == '__main__':
    unittest.main()
