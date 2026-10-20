#!/usr/bin/python3
"""Install fixed upstream routing without changing TLS, ModSecurity or listener ports."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

if os.geteuid() != 0:
    sys.exit('Run with sudo')
source = Path(__file__).resolve().parent
helper = Path('/usr/local/sbin/ieum-bluegreen-nginx')
site = Path('/etc/nginx/sites-available/ieum-front.conf')
backups = Path('/var/lib/ieum-bluegreen/backups')
backups.mkdir(parents=True, exist_ok=True, mode=0o700)
original = site.read_text()
if 'proxy_pass http://ieum_backend' not in original:
    backup = backups / ('ieum-front.' + time.strftime('%Y%m%dT%H%M%S') + '.conf')
    shutil.copy2(site, backup)
    changed = original.replace('http://10.10.10.11:8080', 'http://ieum_backend')
    changed = changed.replace('http://10.10.10.14:8000', 'http://ieum_agent')
    changed = changed.replace('http://10.10.10.10;', 'http://ieum_frontend;')
    changed = changed.replace('proxy_set_header Connection $http_connection;',
                              'proxy_set_header Connection $ieum_connection_upgrade;')
    changed = changed.replace('    add_header X-Content-Type-Options',
                              '    add_header X-IEUM-Environment $ieum_environment always;\n    add_header X-Content-Type-Options')
    changed = changed.replace('    location /api/ {',
        '    location /api/ {\n        proxy_buffering off;\n        proxy_read_timeout 600s;')
    # Old browser tabs may still request assets from the previously active release.
    changed = changed.replace('    location / {\n        proxy_pass http://ieum_frontend;', '''    location /assets/ {
        proxy_pass http://ieum_frontend;
        proxy_intercept_errors on;
        error_page 404 = @ieum_previous_assets;
    }

    location @ieum_previous_assets {
        proxy_pass http://ieum_frontend_previous;
    }

    location / {
        proxy_pass http://ieum_frontend;''')
    if changed == original or '10.10.10.11:8080' in changed:
        sys.exit('Unexpected Nginx site: refusing to install')
else:
    changed = original
shutil.copyfile(source / 'nginx-helper.py', helper)
os.chown(helper, 0, 0)
os.chmod(helper, 0o755)
spec = importlib.util.spec_from_file_location('helper', helper)
# An extension-less executable needs an explicit Python source loader.
from importlib.machinery import SourceFileLoader
module = SourceFileLoader('bg_helper', str(helper)).load_module()
config = module.DESTINATION
old_upstream = config.read_text() if config.exists() else None
if old_upstream is None:
    module.atomic_write(config, module.render('legacy'))
try:
    module.atomic_write(site, changed)
    subprocess.run(['/usr/sbin/nginx', '-t'], check=True)
    rule = Path('/etc/sudoers.d/ieum-bluegreen')
    rule.write_text('kwung ALL=(root) NOPASSWD: /usr/local/sbin/ieum-bluegreen-nginx legacy, /usr/local/sbin/ieum-bluegreen-nginx blue, /usr/local/sbin/ieum-bluegreen-nginx green\n')
    os.chmod(rule, 0o440)
    subprocess.run(['/usr/sbin/visudo', '-cf', str(rule)], check=True)
    subprocess.run(['/usr/bin/systemctl', 'reload', 'nginx'], check=True)
except Exception:
    module.atomic_write(site, original)
    if old_upstream is None:
        config.unlink(missing_ok=True)
    else:
        module.atomic_write(config, old_upstream)
    subprocess.run(['/usr/bin/systemctl', 'reload', 'nginx'])
    raise
print('Installed Nginx routing; current environment preserved')
