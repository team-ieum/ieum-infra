#!/usr/bin/python3
"""Root-owned helper: only switches between three fixed, validated upstream sets."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROUTES = {
    'legacy': ('10.10.10.10', '10.10.10.11', '10.10.10.14'),
    'blue': ('10.10.10.20', '10.10.10.21', '10.10.10.24'),
    'green': ('10.10.10.30', '10.10.10.31', '10.10.10.34'),
}
DESTINATION = Path('/etc/nginx/conf.d/ieum-bluegreen.conf')

def render(color):
    if color not in ROUTES:
        raise ValueError('Expected legacy, blue, or green')
    front, back, agent = ROUTES[color]
    other = 'green' if color == 'blue' else 'blue'
    fallback = ROUTES[other][0]
    return f'''# Managed by ieum-bluegreen-nginx. active={color}
map $host $ieum_environment {{ default {color}; }}
map $http_upgrade $ieum_connection_upgrade {{ default upgrade; '' close; }}
upstream ieum_frontend {{ server {front}:80; }}
upstream ieum_frontend_previous {{ server {fallback}:80; }}
upstream ieum_backend {{ server {back}:8080; }}
upstream ieum_agent {{ server {agent}:8000; }}
'''

def atomic_write(path, content):
    fd, name = tempfile.mkstemp(prefix='.ieum-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(name, 0o644)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)

def apply(color):
    previous = DESTINATION.read_text() if DESTINATION.exists() else None
    atomic_write(DESTINATION, render(color))
    try:
        subprocess.run(['/usr/sbin/nginx', '-t'], check=True, capture_output=True)
        subprocess.run(['/usr/bin/systemctl', 'reload', 'nginx'], check=True, capture_output=True)
    except subprocess.CalledProcessError as error:
        if previous is not None:
            atomic_write(DESTINATION, previous)
            subprocess.run(['/usr/bin/systemctl', 'reload', 'nginx'], capture_output=True)
        else:
            DESTINATION.unlink(missing_ok=True)
        sys.stderr.write(error.stderr.decode(errors='replace') if isinstance(error.stderr, bytes) else str(error))
        raise

if __name__ == '__main__':
    if os.geteuid() != 0 or len(sys.argv) != 2 or sys.argv[1] not in ROUTES:
        sys.exit('Usage: sudo ieum-bluegreen-nginx legacy|blue|green')
    apply(sys.argv[1])
    print('Nginx active environment:', sys.argv[1])
