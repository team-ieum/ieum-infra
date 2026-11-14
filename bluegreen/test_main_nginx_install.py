"""MAIN 설치의 파일 경계와 롤백을 임시 파일시스템에서 검증합니다."""

import importlib.util
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch


spec = importlib.util.spec_from_file_location(
    'main_nginx_installer', Path(__file__).with_name('install-nginx.py'))
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
helper_spec = importlib.util.spec_from_file_location(
    'main_nginx_install_helper', Path(__file__).with_name('nginx-helper.py'))
helper = importlib.util.module_from_spec(helper_spec)
helper_spec.loader.exec_module(helper)
helper.configure_environment('main')

DEV_SITE = '''server {
    listen 11111 ssl default_server;
    server_name green.kiacms.co.kr;
    ssl_certificate /existing/fullchain.pem;
    ssl_certificate_key /existing/privkey.pem;
    modsecurity on;
    modsecurity_rules_file /etc/nginx/modsec/main.conf;
    add_header X-Content-Type-Options nosniff always;
    access_log /var/log/nginx/ieum_access.log;
    error_log /var/log/nginx/ieum_error.log;
    location /api/ {
        proxy_pass http://10.10.10.11:8080;
    }
    location /agent/ {
        proxy_pass http://10.10.10.14:8000;
    }
    location / {
        proxy_pass http://10.10.10.10;
    }
}
'''

OTHER_DEV_SERVERS = '''# HTTP and TLS default/redirect policy must stay only in DEV.
server {
    listen 80 default_server;
    server_name _;
    return 444;
}
server {
    listen 80;
    server_name green.kiacms.co.kr;
    return 301 https://green.kiacms.co.kr$request_uri;
}
server {
    listen 11111 ssl default_server;
    server_name _;
    ssl_certificate /existing/default.pem;
    ssl_certificate_key /existing/default-key.pem;
    return 444;
}
'''
MULTI_DEV_SITE = OTHER_DEV_SERVERS + DEV_SITE.replace(
    'listen 11111 ssl default_server;', 'listen 11111 ssl;').replace(
        'server_name green.kiacms.co.kr;', 'server_name green.kiacms.co.kr 192.168.0.11;')


class MainSiteSelectionTest(unittest.TestCase):
    def test_existing_single_server_fixture_remains_supported(self):
        self.assertEqual(installer.select_dev_tls_site(DEV_SITE), DEV_SITE)
        rendered = installer.render_main_site(DEV_SITE)
        self.assertIn('server_name kiacms.co.kr;', rendered)
        self.assertNotIn('default_server', rendered)
        self.assertIn('access_log /var/log/nginx/ieum-main_access.log;', rendered)
        self.assertIn('error_log /var/log/nginx/ieum-main_error.log;', rendered)

    def test_four_server_live_shape_selects_only_the_named_tls_server(self):
        selected = installer.select_dev_tls_site(MULTI_DEV_SITE)
        self.assertEqual(selected, MULTI_DEV_SITE[len(OTHER_DEV_SERVERS):])
        rendered = installer.render_main_site(MULTI_DEV_SITE)
        for excluded in ('listen 80', 'return 444', 'return 301', 'default_server',
                         '/existing/default.pem', '192.168.0.11'):
            self.assertNotIn(excluded, rendered)
        self.assertEqual(len(installer.nginx_directives(rendered)), 1)

    def test_inline_server_name_still_installs_main_legacy_guard(self):
        original = MULTI_DEV_SITE.replace(
            'listen 11111 ssl;\n    server_name green.kiacms.co.kr 192.168.0.11;',
            'listen 11111 ssl; server_name green.kiacms.co.kr 192.168.0.11;')
        rendered = installer.render_main_site(original)
        directives = installer.nginx_directives(rendered)[0]['children']
        self.assertEqual([node['words'] for node in directives if node['words'][0] == 'server_name'],
                         [['server_name', 'kiacms.co.kr']])
        self.assertIn('add_header X-IEUM-Deployment main always;', rendered)
        self.assertIn('if ($ieum_main_environment = legacy) { return 503; }', rendered)
        self.assertNotIn('192.168.0.11', rendered)

    def test_comments_quotes_escapes_and_variable_braces_do_not_change_block_boundaries(self):
        policy = '''    # } server { server_name kiacms.co.kr; }
    add_header X-Quoted "} # { ; \\"still quoted\\"";
    add_header X-Single '} # { ;';
    set $escaped \\}\\{;
    set $url ${scheme}://$host;
    if ($request_method = OPTIONS) { return 204; }
'''
        selected = DEV_SITE.replace('    modsecurity on;\n', policy + '    modsecurity on;\n')
        original = '# server { unmatched comment brace\n' + OTHER_DEV_SERVERS + selected
        self.assertEqual(installer.select_dev_tls_site(original), selected)
        self.assertIn(policy, installer.render_main_site(original))

    def test_multiple_named_tls_candidates_are_rejected(self):
        for second in (DEV_SITE, DEV_SITE.replace('    ssl_certificate /existing/fullchain.pem;\n', ''),
                       DEV_SITE.replace('server_name green.kiacms.co.kr;',
                                        'server_name green.kiacms.co.kr; server_name another.host;')):
            with self.subTest(second=second), self.assertRaisesRegex(RuntimeError, 'exactly one named'):
                installer.render_main_site(MULTI_DEV_SITE + second)

    def test_missing_named_tls_candidate_is_rejected(self):
        for original in (OTHER_DEV_SERVERS, DEV_SITE.replace('11111', '443'),
                         DEV_SITE.replace('green.kiacms.co.kr', 'other.kiacms.co.kr'),
                         DEV_SITE.replace('    ssl_certificate /existing/fullchain.pem;\n', '')):
            with self.subTest(original=original), self.assertRaisesRegex(RuntimeError, 'exactly one named'):
                installer.render_main_site(original)

    def test_exact_main_hostname_in_another_server_is_rejected(self):
        apex = 'server { listen 80; server_name "kiacms.co.kr"; return 301 https://$host$request_uri; }\n'
        for original in (apex + MULTI_DEV_SITE, MULTI_DEV_SITE + apex,
                         MULTI_DEV_SITE + apex.replace('kiacms.co.kr', 'KIACMS.CO.KR')):
            with self.subTest(original=original), self.assertRaisesRegex(RuntimeError, 'exact MAIN hostname'):
                installer.render_main_site(original)

    def test_unclosed_quotes_or_blocks_are_rejected(self):
        for original in (DEV_SITE + 'server {', DEV_SITE + 'add_header Broken "unterminated',
                         DEV_SITE + '}', DEV_SITE + 'listen 80'):
            with self.subTest(original=original), self.assertRaises(RuntimeError):
                installer.render_main_site(original)


class MainNginxInstallationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.owner = SimpleNamespace(pw_uid=os.geteuid(), pw_gid=os.getegid())
        self.lock = '/run/lock/ieum-bluegreen-nginx.lock'
        self.chmod = os.chmod
        self.files = {
            'site': self.mapped('/etc/nginx/sites-available/ieum-main.conf'),
            'enabled': self.mapped('/etc/nginx/sites-enabled/ieum-main.conf'),
            'helper': self.mapped('/usr/local/sbin/ieum-main-bluegreen-nginx'),
            'rule': self.mapped('/etc/sudoers.d/ieum-main-bluegreen'),
            'upstream': self.mapped('/etc/nginx/conf.d/ieum-main-bluegreen.conf'),
        }
        for path in self.files.values():
            path.parent.mkdir(parents=True, exist_ok=True)
        self.mapped('/var/lib').mkdir(parents=True)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.new_helper = '# Reviewed MAIN and DEV helper with shared lock support\n'
        (self.source / 'nginx-helper.py').write_text(self.new_helper)
        self.dev_helper = self.mapped('/usr/local/sbin/ieum-bluegreen-nginx')
        protected = {
            '/etc/nginx/sites-available/ieum-front.conf': MULTI_DEV_SITE,
            '/etc/nginx/conf.d/ieum-bluegreen.conf': '# DEV active=green\n',
            '/etc/nginx/nginx.conf': 'worker_shutdown_timeout 600s;\nevents {}\n',
            '/etc/sudoers.d/ieum-bluegreen': '# Existing DEV privileges\n',
            '/usr/local/sbin/ieum-bluegreen-nginx': 'def nginx_lock():\n    pass\n# ' + self.lock + '\n',
        }
        for name, content in protected.items():
            path = self.mapped(name)
            path.write_text(content)
            self.chmod(path, 0o640)
        self.protected = {self.mapped(name): self.snapshot(self.mapped(name)) for name in protected}
        self.module = SimpleNamespace(
            DESTINATION=self.files['upstream'], NGINX_LOCK=Path(self.lock),
            atomic_write=Mock(side_effect=self.atomic_write),
            render=Mock(side_effect=lambda color: '# Managed by ieum-bluegreen-nginx. active=' + color + '\n'
                        'upstream ieum_main_frontend { server 127.0.0.1:9; }\n'),
            validate_nginx=lambda: helper.validate_nginx(),
        )
        patches = (
            patch.object(installer, 'Path', side_effect=self.mapped),
            patch.object(installer, '__file__', str(self.source / 'install-nginx.py')),
            patch.object(installer.os, 'chown', side_effect=self.safe_chown),
            patch.object(installer.os, 'chmod', side_effect=self.safe_chmod),
            patch.object(installer.subprocess, 'run', side_effect=self.run_command),
            patch('builtins.print'),
        )
        started = [replacement.start() for replacement in patches]
        for replacement in patches:
            self.addCleanup(replacement.stop)
        self.chown_mock, self.command_mock = started[2], started[4]
        self.fail_command = None
        self.failure_injected = False
        self.nginx_warning = None

    def mapped(self, value):
        path = Path(value)
        if path == self.root or self.root in path.parents:
            return path
        if path.is_absolute() and path.parts[1] in ('etc', 'usr', 'var'):
            return self.root / path.relative_to('/')
        raise AssertionError('Test attempted to access a path outside its temporary filesystem')

    def assert_temporary(self, path):
        self.assertIn(self.root, Path(path).parents)

    def safe_chown(self, path, _uid, _gid):
        # 실제 root 소유권 변경은 수행하지 않습니다. 복원 값은 mock 호출로 검사합니다.
        self.assert_temporary(path)

    def safe_chmod(self, path, mode, **options):
        self.assert_temporary(path)
        self.chmod(path, mode, **options)

    def atomic_write(self, path, content):
        self.assert_temporary(path)
        # 운영 helper처럼 새 inode로 교체해 기존 권한을 잃는 상황도 재현합니다.
        descriptor, name = tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(descriptor, 'w') as stream:
                stream.write(content)
            self.chmod(name, 0o644)
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)

    def run_command(self, arguments, **options):
        allowed = (
            ['/usr/sbin/visudo', '-cf', str(self.files['rule'])],
            ['/usr/sbin/nginx', '-t'],
            ['/usr/bin/systemctl', 'reload', 'nginx'],
        )
        self.assertIn(arguments, allowed)
        if arguments == self.fail_command and not self.failure_injected:
            self.failure_injected = True
            self.assertTrue(options.get('check'))
            raise subprocess.CalledProcessError(1, arguments)
        if arguments == ['/usr/sbin/nginx', '-t'] and self.nginx_warning:
            self.failure_injected = True
            return subprocess.CompletedProcess(arguments, 0, stdout=b'', stderr=self.nginx_warning)
        return subprocess.CompletedProcess(arguments, 0, stdout=b'', stderr=b'')

    @staticmethod
    def snapshot(path):
        metadata = path.lstat()
        content = os.readlink(path) if path.is_symlink() else path.read_bytes()
        return content, stat.S_IMODE(metadata.st_mode), metadata.st_uid, metadata.st_gid

    def configuration_snapshot(self):
        return {path: self.snapshot(path)
                for directory in (self.root / 'etc', self.root / 'usr')
                for path in directory.rglob('*') if path.is_symlink() or path.is_file()}

    def existing_main(self):
        originals = {
            'site': ('# Managed MAIN site; reviewed policy\n# Preserve custom MAIN policy\n', 0o640),
            'helper': ('# Old reviewed MAIN helper\n', 0o750),
            'rule': ('# Old MAIN sudo rules\n', 0o440),
            'upstream': ('# Managed by ieum-bluegreen-nginx. active=green\n# Old MAIN routes\n', 0o600),
        }
        for name, (content, mode) in originals.items():
            self.files[name].write_text(content)
            self.chmod(self.files[name], mode)

    def assert_dev_preserved(self):
        for path, expected in self.protected.items():
            self.assertEqual(self.snapshot(path), expected, str(path))

    def test_fresh_install_writes_only_main_routes_and_preserves_dev_and_global_nginx(self):
        installer.install_main_locked(self.owner, self.module)
        self.assert_dev_preserved()
        self.assertEqual({entry.args[0] for entry in self.module.atomic_write.call_args_list},
                         {self.files[name] for name in ('site', 'helper', 'rule', 'upstream')})
        self.assertEqual(self.files['enabled'].resolve(), self.files['site'])
        self.assertEqual(self.files['helper'].read_text(), self.new_helper)
        self.assertEqual(stat.S_IMODE(self.files['helper'].stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(self.files['rule'].stat().st_mode), 0o440)
        self.module.render.assert_called_once_with('legacy')
        site = self.files['site'].read_text()
        for expected in ('server_name kiacms.co.kr;', 'if ($ieum_main_environment = legacy) { return 503; }',
                         'add_header X-IEUM-Deployment main always;',
                         'http://ieum_main_frontend', 'http://ieum_main_backend', 'http://ieum_main_agent',
                         'modsecurity_rules_file /etc/nginx/modsec/main.conf;',
                         'ssl_certificate /existing/fullchain.pem;'):
            self.assertIn(expected, site)
        self.assertNotIn('default_server', site)
        self.assertNotIn('green.kiacms.co.kr', site)
        self.assertNotIn('listen 80', site)
        self.assertNotIn('return 444', site)
        self.assertNotIn('return 301', site)
        self.assertEqual(self.command_mock.call_args_list, [
            call(['/usr/sbin/visudo', '-cf', str(self.files['rule'])], check=True, capture_output=True),
            call(['/usr/sbin/nginx', '-t'], check=True, capture_output=True),
            call(['/usr/bin/systemctl', 'reload', 'nginx'], check=True, capture_output=True),
        ])

    def test_reinstallation_preserves_reviewed_main_site_active_color_and_existing_activation(self):
        self.existing_main()
        self.files['enabled'].symlink_to(self.files['site'])
        original_site = self.files['site'].read_bytes()
        original_link = self.files['enabled'].lstat().st_ino
        installer.install_main_locked(self.owner, self.module)
        self.assertEqual(self.files['site'].read_bytes(), original_site)
        self.assertEqual(self.files['enabled'].lstat().st_ino, original_link)
        self.module.render.assert_called_once_with('green')
        self.assert_dev_preserved()

    def assert_failed_install_restored(self, command, already_enabled=False, warning=None):
        self.existing_main()
        if already_enabled:
            self.files['enabled'].symlink_to(self.files['site'])
        before = self.configuration_snapshot()
        self.fail_command = command if warning is None else None
        self.nginx_warning = warning
        with self.assertRaises(subprocess.CalledProcessError):
            installer.install_main_locked(self.owner, self.module)
        self.assertTrue(self.failure_injected)
        self.assertEqual(self.configuration_snapshot(), before)
        self.assertEqual(os.path.lexists(self.files['enabled']), already_enabled)
        for name in ('site', 'helper', 'rule', 'upstream'):
            path = self.files[name]
            _, _, uid, gid = before[path]
            self.assertIn(call(path, uid, gid), self.chown_mock.call_args_list)
        self.assertEqual(self.command_mock.call_args_list[-1],
                         call(['/usr/bin/systemctl', 'reload', 'nginx'], capture_output=True))
        self.assert_dev_preserved()

    def test_nginx_validation_failure_restores_main_content_permissions_and_removes_new_activation(self):
        self.assert_failed_install_restored(['/usr/sbin/nginx', '-t'])

    def test_reload_failure_restores_main_content_permissions_and_removes_new_activation(self):
        self.assert_failed_install_restored(['/usr/bin/systemctl', 'reload', 'nginx'])

    def test_reload_failure_preserves_existing_main_activation(self):
        self.assert_failed_install_restored(['/usr/bin/systemctl', 'reload', 'nginx'], already_enabled=True)

    def test_successful_nginx_exit_with_conflicting_server_warning_rolls_back_main_installation(self):
        self.assert_failed_install_restored(['/usr/sbin/nginx', '-t'], warning=(
            b'nginx: [warn] conflicting server name "kiacms.co.kr" on 0.0.0.0:11111, ignored\n'
            b'nginx: configuration file /etc/nginx/nginx.conf test is successful\n'))
        reloads = [entry for entry in self.command_mock.call_args_list
                   if entry.args[0] == ['/usr/bin/systemctl', 'reload', 'nginx']]
        self.assertEqual(reloads, [call(['/usr/bin/systemctl', 'reload', 'nginx'], capture_output=True)])

    def test_first_install_validation_failure_removes_all_new_main_configuration_files(self):
        before = self.configuration_snapshot()
        self.fail_command = ['/usr/sbin/nginx', '-t']
        with self.assertRaises(subprocess.CalledProcessError):
            installer.install_main_locked(self.owner, self.module)
        self.assertEqual(self.configuration_snapshot(), before)
        self.assertTrue(all(not os.path.lexists(path) for path in self.files.values()))
        self.assert_dev_preserved()

    def assert_rejected_before_mutation(self, message):
        before = self.configuration_snapshot()
        with self.assertRaisesRegex(RuntimeError, message):
            installer.install_main_locked(self.owner, self.module)
        self.assertEqual(self.configuration_snapshot(), before)
        self.assertFalse(self.mapped('/var/lib/ieum-main-bluegreen').exists())
        self.module.atomic_write.assert_not_called()
        self.module.render.assert_not_called()
        self.chown_mock.assert_not_called()
        self.command_mock.assert_not_called()

    def test_unmanaged_main_site_is_rejected_before_any_mutation(self):
        self.files['site'].write_text('# Operator-owned unrelated site\n')
        self.assert_rejected_before_mutation('Unmanaged MAIN site')

    def test_dev_site_owning_exact_main_hostname_is_rejected_before_any_mutation(self):
        self.mapped('/etc/nginx/sites-available/ieum-front.conf').write_text(
            DEV_SITE.replace('server_name green.kiacms.co.kr;',
                             'server_name green.kiacms.co.kr kiacms.co.kr;'))
        self.assert_rejected_before_mutation('exact MAIN hostname')

    def test_other_server_owning_exact_main_hostname_is_rejected_before_any_mutation(self):
        self.mapped('/etc/nginx/sites-available/ieum-front.conf').write_text(
            MULTI_DEV_SITE + 'server { listen 80; server_name kiacms.co.kr; return 444; }\n')
        self.assert_rejected_before_mutation('exact MAIN hostname')

    def test_ambiguous_dev_tls_servers_are_rejected_before_any_mutation(self):
        self.mapped('/etc/nginx/sites-available/ieum-front.conf').write_text(MULTI_DEV_SITE + DEV_SITE)
        self.assert_rejected_before_mutation('exactly one named')

    def test_activation_symlink_to_another_site_is_rejected_before_any_mutation(self):
        self.files['enabled'].symlink_to(self.mapped('/etc/nginx/sites-available/ieum-front.conf'))
        self.assert_rejected_before_mutation('activation path belongs')

    def test_regular_file_at_activation_path_is_rejected_before_any_mutation(self):
        self.files['enabled'].write_text('# Operator-owned enabled configuration\n')
        self.assert_rejected_before_mutation('activation path belongs')

    def test_missing_dev_helper_is_rejected_before_any_mutation(self):
        self.dev_helper.unlink()
        self.assert_rejected_before_mutation('shared-lock DEV helper')

    def test_dev_helper_without_shared_lock_is_rejected_before_any_mutation(self):
        self.dev_helper.write_text('# Old helper without a lock\n')
        self.assert_rejected_before_mutation('shared-lock DEV helper')

    def test_dev_helper_with_different_lock_is_rejected_before_any_mutation(self):
        self.dev_helper.write_text('def nginx_lock():\n    pass\n# /run/lock/some-other-lock\n')
        self.assert_rejected_before_mutation('shared-lock DEV helper')


if __name__ == '__main__':
    unittest.main()
