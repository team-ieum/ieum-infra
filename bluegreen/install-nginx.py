#!/usr/bin/python3
"""Install routing and persistent asset fallback, preserving existing TLS/WAF settings."""
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
import contextlib
import argparse
import fcntl
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import subprocess
import time

DEPLOYMENT_HOME = Path(os.environ.get('IEUM_BLUEGREEN_HOME', '/home/kwung/deploy/bluegreen'))
ENVIRONMENT = 'dev'

PREVIOUS_ARCHIVE_ASSET_LOCATIONS = '''    location ^~ /assets/ {
        proxy_pass http://ieum_frontend;
        proxy_intercept_errors on;
        error_page 404 502 503 504 = @ieum_archived_assets;
    }

    location @ieum_archived_assets {
        root /var/lib/ieum-bluegreen;
        try_files $uri =404;
        add_header Cache-Control "public, max-age=2592000, immutable";
    }

'''

ASSET_LOCATIONS = '''    location ^~ /assets/ {
        proxy_pass http://ieum_frontend;
        proxy_intercept_errors on;
        error_page 404 502 503 504 = @ieum_archived_assets;
    }

    location @ieum_archived_assets {
        root /var/lib/ieum-bluegreen;
        try_files $uri =404;
        expires 30d;
    }

'''

LEGACY_ASSET_LOCATIONS = '''    location /assets/ {
        proxy_pass http://ieum_frontend;
        proxy_intercept_errors on;
        error_page 404 = @ieum_previous_assets;
    }

    location @ieum_previous_assets {
        proxy_pass http://ieum_frontend_previous;
    }

'''

def render_site(original):
    changed = original.replace('http://10.10.10.11:8080', 'http://ieum_backend')
    changed = changed.replace('http://10.10.10.14:8000', 'http://ieum_agent')
    changed = changed.replace('http://10.10.10.10;', 'http://ieum_frontend;')
    changed = changed.replace('proxy_set_header Connection $http_connection;',
                              'proxy_set_header Connection $ieum_connection_upgrade;')
    if 'add_header X-IEUM-Environment' not in changed:
        changed = changed.replace('    add_header X-Content-Type-Options',
            '    add_header X-IEUM-Environment $ieum_environment always;\n    add_header X-Content-Type-Options')
    if 'proxy_buffering off;' not in changed:
        changed = changed.replace('    location /api/ {',
            '    location /api/ {\n        proxy_buffering off;\n        proxy_read_timeout 600s;')
    # Upgrade either our original fallback or our current configuration idempotently.
    # expires sets cache freshness without suppressing inherited add_header policy.
    changed = changed.replace(LEGACY_ASSET_LOCATIONS, '')
    changed = changed.replace(PREVIOUS_ARCHIVE_ASSET_LOCATIONS, '').replace(ASSET_LOCATIONS, '')
    if re.search(r'location\s+(?:\^~\s+)?/assets/|location\s+@ieum_(?:previous|archived)_assets', changed):
        raise RuntimeError('Unrecognized custom asset locations; preserve and review them before installation')
    root_location = '    location / {\n        proxy_pass http://ieum_frontend;'
    if changed.count(root_location) != 1 or 'proxy_pass http://ieum_backend' not in changed:
        raise RuntimeError('Unexpected Nginx site: refusing to install')
    return changed.replace(root_location, ASSET_LOCATIONS + root_location)

def render_main(original):
    if re.search(r'^\s*worker_shutdown_timeout\s+', original, re.MULTILINE):
        return original
    return 'worker_shutdown_timeout 600s;\n' + original

def nginx_directives(original):
    """Read directive boundaries without treating quoted/comment braces as blocks."""
    tokens = []
    index = 0
    while index < len(original):
        if original[index].isspace():
            index += 1
            continue
        if original[index] == '#':
            newline = original.find('\n', index)
            index = len(original) if newline < 0 else newline + 1
            continue
        start = index
        if original[index] in '{};':
            tokens.append((original[index], original[index], start, index + 1))
            index += 1
            continue
        value, quote = '', None
        while index < len(original):
            char = original[index]
            if char == '\\':
                if index + 1 == len(original):
                    raise RuntimeError('Incomplete escape in Nginx site')
                value += original[index + 1]
                index += 2
            elif quote:
                if char == quote:
                    quote = None
                else:
                    value += char
                index += 1
            elif char in ('"', "'"):
                quote = char
                index += 1
            elif original.startswith('${', index):
                end = original.find('}', index + 2)
                if end < 0:
                    raise RuntimeError('Incomplete variable in Nginx site')
                value += original[index:end + 1]
                index = end + 1
            elif char.isspace() or char in '{};#':
                break
            else:
                value += char
                index += 1
        if quote:
            raise RuntimeError('Unclosed quote in Nginx site')
        tokens.append(('word', value, start, index))

    root = {'children': []}
    stack, words, start = [root], [], None
    for kind, value, begin, end in tokens:
        if kind == 'word':
            if not words:
                start = begin
            words.append(value)
        elif kind in (';', '{'):
            if not words:
                raise RuntimeError('Unexpected directive boundary in Nginx site')
            node = {'words': words, 'start': start, 'end': end,
                    'children': [] if kind == '{' else None}
            stack[-1]['children'].append(node)
            if kind == '{':
                stack.append(node)
            words = []
        else:
            if words or len(stack) == 1:
                raise RuntimeError('Unexpected closing brace in Nginx site')
            stack.pop()['end'] = end
    if words or len(stack) != 1:
        raise RuntimeError('Incomplete directive or block in Nginx site')
    return root['children']

def select_dev_tls_site(original):
    """Select the named DEV TLS server, leaving every other server out of MAIN."""
    servers = [node for node in nginx_directives(original)
               if node['words'] == ['server'] and node['children'] is not None]
    matches = []
    for server in servers:
        directives = [node['words'] for node in server['children'] if node['children'] is None]
        names = [words[1:] for words in directives if words[0] == 'server_name']
        if any(name.lower() == 'kiacms.co.kr' for group in names for name in group):
            raise RuntimeError('DEV site owns the exact MAIN hostname; review its server_name while preserving green.kiacms.co.kr before installing MAIN')
        tls = any(words[0] == 'listen' and len(words) > 2 and 'ssl' in words[2:]
                  and (words[1] == '11111' or words[1].endswith(':11111')) for words in directives)
        certificate = any(words[0] == 'ssl_certificate' and len(words) == 2 for words in directives)
        if any(name.lower() == 'green.kiacms.co.kr' for group in names for name in group) and tls:
            matches.append((server, names, certificate))
    if len(matches) != 1 or len(matches[0][1]) != 1 or not matches[0][2]:
        raise RuntimeError('MAIN installation requires exactly one named green.kiacms.co.kr TLS server on port 11111')
    server = matches[0][0]
    return original[server['start']:server['end']] + '\n'

def render_main_site(original):
    """Derive MAIN from the reviewed TLS/WAF policy without rewriting the DEV site."""
    rendered = render_site(select_dev_tls_site(original))
    name_directive = next(node for node in nginx_directives(rendered)[0]['children']
                          if node['children'] is None and node['words'][0] == 'server_name')
    rendered = (rendered[:name_directive['start']] + 'server_name kiacms.co.kr;\n'
        '    add_header X-IEUM-Deployment main always;\n'
        '    if ($ieum_main_environment = legacy) { return 503; }' + rendered[name_directive['end']:])
    rendered = re.sub(r'(^\s*listen\s+[^;]*)\bdefault_server\b', r'\1', rendered, flags=re.MULTILINE)
    rendered = rendered.replace('green.kiacms.co.kr', 'kiacms.co.kr')
    rendered = rendered.replace('/var/lib/ieum-bluegreen', '/var/lib/ieum-main-bluegreen')
    for symbol in ('environment', 'connection_upgrade', 'frontend', 'backend', 'agent', 'archived_assets'):
        rendered = re.sub(r'\bieum_' + symbol + r'\b', 'ieum_main_' + symbol, rendered)
    for directive, filename in [('access_log', 'ieum-main_access.log'), ('error_log', 'ieum-main_error.log')]:
        rendered = re.sub(r'(^\s*' + directive + r'\s+)[^\s;]+',
                          r'\g<1>/var/log/nginx/' + filename, rendered, flags=re.MULTILINE)
    if 'http://ieum_backend' in rendered or 'http://ieum_frontend' in rendered:
        raise RuntimeError('MAIN site still references DEV upstreams')
    return '# Managed MAIN site; derived from the reviewed DEV TLS/WAF policy.\n' + rendered

def install_main_locked(owner, module):
    source = Path(__file__).resolve().parent
    site = Path('/etc/nginx/sites-available/ieum-main.conf')
    enabled = Path('/etc/nginx/sites-enabled/ieum-main.conf')
    helper = Path('/usr/local/sbin/ieum-main-bluegreen-nginx')
    rule = Path('/etc/sudoers.d/ieum-main-bluegreen')
    destination = module.DESTINATION
    if site.exists() and not site.read_text().startswith('# Managed MAIN site;'):
        raise RuntimeError('Unmanaged MAIN site already exists; review it before installation')
    if os.path.lexists(enabled) and (not enabled.is_symlink() or enabled.resolve() != site):
        raise RuntimeError('MAIN site activation path belongs to another configuration')
    # The old DEV helper must participate in the same lock before MAIN is enabled.
    dev_helper = Path('/usr/local/sbin/ieum-bluegreen-nginx')
    if not dev_helper.exists() or 'def nginx_lock():' not in dev_helper.read_text() or \
            str(module.NGINX_LOCK) not in dev_helper.read_text():
        raise RuntimeError('Install the reviewed shared-lock DEV helper and update its SHA pins first')
    original = Path('/etc/nginx/sites-available/ieum-front.conf').read_text()
    rendered = site.read_text() if site.exists() else render_main_site(original)
    base = Path('/var/lib/ieum-main-bluegreen')
    base.mkdir(exist_ok=True, mode=0o755)
    for name, mode in [('assets', 0o755), ('staging', 0o700)]:
        path = base / name
        if path.is_symlink(): raise RuntimeError('MAIN asset directory must not be a symlink')
        path.mkdir(exist_ok=True, mode=mode)
        os.chown(path, owner.pw_uid, owner.pw_gid)
        os.chmod(path, mode)
    paths = (site, helper, rule, destination)
    saved = {p: (p.read_bytes(), p.stat()) if p.exists() else None for p in paths}
    had_enabled = os.path.lexists(enabled)
    backup = base / 'backups' / time.strftime('%Y%m%dT%H%M%S')
    backup.mkdir(parents=True, mode=0o700)
    os.chmod(backup.parent, 0o700)
    for path in paths:
        if path.exists(): shutil.copy2(path, backup / path.name)
    try:
        module.atomic_write(helper, (source / 'nginx-helper.py').read_text())
        os.chown(helper, 0, 0); os.chmod(helper, 0o755)
        previous_color = 'legacy'
        if destination.exists():
            match = re.search(r'^# Managed by ieum-bluegreen-nginx\. active=(legacy|blue|green)$',
                              destination.read_text(), re.MULTILINE)
            if not match: raise RuntimeError('Unmanaged MAIN route already exists')
            previous_color = match[1]
        module.atomic_write(destination, module.render(previous_color))
        module.atomic_write(site, rendered)
        module.atomic_write(rule, 'kwung ALL=(root) NOPASSWD: ' + ', '.join(
            str(helper) + ' ' + command for command in ('legacy', 'blue', 'green', 'configure-frontend')) + '\n')
        os.chmod(rule, 0o440)
        if not had_enabled: enabled.symlink_to(site)
        subprocess.run(['/usr/sbin/visudo', '-cf', str(rule)], check=True, capture_output=True)
        module.validate_nginx()
        subprocess.run(['/usr/bin/systemctl', 'reload', 'nginx'], check=True, capture_output=True)
    except Exception:
        if not had_enabled: enabled.unlink(missing_ok=True)
        for path, previous in saved.items():
            if previous is None: path.unlink(missing_ok=True)
            else:
                content, metadata = previous
                module.atomic_write(path, content.decode())
                os.chown(path, metadata.st_uid, metadata.st_gid)
                os.chmod(path, stat.S_IMODE(metadata.st_mode))
        restored = subprocess.run(['/usr/bin/systemctl', 'reload', 'nginx'], capture_output=True)
        if restored.returncode: raise RuntimeError('MAIN installation rollback reload failed')
        raise
    print('Installed independent MAIN routes; DEV site and upstream files were preserved')

@contextlib.contextmanager
def deployment_lock(owner):
    # Do not create a root-owned deployment directory that the controller cannot use.
    try:
        directory = DEPLOYMENT_HOME.lstat()
    except FileNotFoundError:
        raise RuntimeError('Create the deployment directory as kwung before installing Nginx routing') from None
    if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != owner.pw_uid:
        raise RuntimeError('Deployment directory must be a real directory owned by kwung')
    path = DEPLOYMENT_HOME / 'deploy.lock'
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        descriptor = os.open(path, flags)
        created = False
    else:
        created = True
    try:
        if created:
            os.fchown(descriptor, owner.pw_uid, owner.pw_gid)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != owner.pw_uid \
                or metadata.st_mode & 0o022:
            raise RuntimeError('Deployment lock must be a regular file owned and writable only by kwung')
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def load_helper():
    source = Path(__file__).resolve().parent
    loader = SourceFileLoader('bg_helper', str(source / 'nginx-helper.py'))
    module = module_from_spec(spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    module.configure_environment(ENVIRONMENT)
    return module


def install_locked(owner):
    module = load_helper()
    with module.nginx_lock():
        if ENVIRONMENT == 'main':
            install_main_locked(owner, module)
        else:
            install_dev_locked(owner, module)


def install_dev_locked(owner, module):
    source = Path(__file__).resolve().parent
    helper = Path('/usr/local/sbin/ieum-bluegreen-nginx')
    site = Path('/etc/nginx/sites-available/ieum-front.conf')
    nginx_main = Path('/etc/nginx/nginx.conf')
    rule = Path('/etc/sudoers.d/ieum-bluegreen')
    destination = Path('/etc/nginx/conf.d/ieum-bluegreen.conf')
    paths = (site, nginx_main, helper, rule, destination)
    saved = {p: (p.read_bytes(), p.stat()) if p.exists() else None for p in paths}
    changed = render_site(site.read_text())
    changed_main = render_main(nginx_main.read_text())
    backups = Path('/var/lib/ieum-bluegreen/backups') / time.strftime('%Y%m%dT%H%M%S')
    backups.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chmod(backups.parent.parent, 0o755)  # Nginx traverses this public archive base.
    os.chmod(backups.parent, 0o700)
    for path in paths:
        if path.exists():
            shutil.copy2(path, backups / path.name)
    for directory, mode in ((backups.parent.parent / 'assets', 0o755),
                            (backups.parent.parent / 'staging', 0o700)):
        directory.mkdir(exist_ok=True, mode=mode)
        os.chown(directory, owner.pw_uid, owner.pw_gid)
        os.chmod(directory, mode)
    try:
        module.atomic_write(helper, (source / 'nginx-helper.py').read_text())
        os.chown(helper, 0, 0)
        os.chmod(helper, 0o755)
        if not destination.exists():
            module.atomic_write(destination, module.render('legacy'))
        else:
            match = re.search(r'active=(legacy|blue|green)', destination.read_text())
            if not match:
                raise RuntimeError('Cannot preserve the current upstream environment')
            module.atomic_write(destination, module.render(match.group(1)))
        module.atomic_write(site, changed)
        module.atomic_write(nginx_main, changed_main)
        module.atomic_write(rule,
            'kwung ALL=(root) NOPASSWD: /usr/local/sbin/ieum-bluegreen-nginx legacy, '
            '/usr/local/sbin/ieum-bluegreen-nginx blue, /usr/local/sbin/ieum-bluegreen-nginx green, '
            '/usr/local/sbin/ieum-bluegreen-nginx configure-frontend\n')
        os.chmod(rule, 0o440)
        subprocess.run(['/usr/sbin/nginx', '-t'], check=True)
        subprocess.run(['/usr/sbin/visudo', '-cf', str(rule)], check=True)
        subprocess.run(['/usr/bin/systemctl', 'reload', 'nginx'], check=True)
    except Exception as original_error:
        for path, previous in saved.items():
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                contents, metadata = previous
                module.atomic_write(path, contents.decode())
                os.chown(path, metadata.st_uid, metadata.st_gid)
                os.chmod(path, metadata.st_mode & 0o7777)
        restored = subprocess.run(['/usr/bin/systemctl', 'reload', 'nginx'])
        if restored.returncode:
            raise RuntimeError('Installation failed and rollback reload failed; inspect Nginx') from original_error
        raise
    print('Installed routing, independent asset archive and bounded Nginx worker shutdown')


def main(argv=None):
    global ENVIRONMENT, DEPLOYMENT_HOME
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--environment', choices=('dev', 'main'), default='dev')
    # Imported callers historically invoke main() under unittest's arguments.
    args = parser.parse_args([] if argv is None else argv)
    ENVIRONMENT = args.environment
    if ENVIRONMENT == 'main':
        DEPLOYMENT_HOME = Path('/home/kwung/deploy/bluegreen-main')
    if os.geteuid() != 0:
        raise SystemExit('Run with sudo')
    owner = pwd.getpwnam('kwung')
    # Lock before reading live configuration and retain the lock through rollback.
    with deployment_lock(owner):
        install_locked(owner)

if __name__ == '__main__':
    import sys
    main(sys.argv[1:])
