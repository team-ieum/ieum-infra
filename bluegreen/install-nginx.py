#!/usr/bin/python3
"""Install routing and persistent asset fallback, preserving existing TLS/WAF settings."""
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import time

ASSET_LOCATIONS = '''    location ^~ /assets/ {
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
    changed = changed.replace(LEGACY_ASSET_LOCATIONS, '').replace(ASSET_LOCATIONS, '')
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

def main():
    if os.geteuid() != 0:
        raise SystemExit('Run with sudo')
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
    owner = pwd.getpwnam('kwung')
    for directory, mode in ((backups.parent.parent / 'assets', 0o755),
                            (backups.parent.parent / 'staging', 0o700)):
        directory.mkdir(exist_ok=True, mode=mode)
        os.chown(directory, owner.pw_uid, owner.pw_gid)
        os.chmod(directory, mode)
    loader = SourceFileLoader('bg_helper', str(source / 'nginx-helper.py'))
    module = module_from_spec(spec_from_loader(loader.name, loader))
    loader.exec_module(module)
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
            '/usr/local/sbin/ieum-bluegreen-nginx blue, /usr/local/sbin/ieum-bluegreen-nginx green\n')
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

if __name__ == '__main__':
    main()
