#!/usr/bin/python3
"""Root-owned helper: switch validated routes using an immutable frontend policy."""
import ipaddress
import contextlib
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys
import tempfile

ROUTES = {
    'legacy': ('10.10.10.10', '10.10.10.11', '10.10.10.14'),
    'blue': ('10.10.10.20', '10.10.10.21', '10.10.10.24'),
    'green': ('10.10.10.30', '10.10.10.31', '10.10.10.34'),
}
DESTINATION = Path('/etc/nginx/conf.d/ieum-bluegreen.conf')
FRONTEND_POLICY = Path('/etc/nginx/ieum-frontend-deployment.json')
FRONTEND_CANDIDATE = Path('/home/kwung/deploy/bluegreen/frontend-deployment-candidate.json')
RUNTIME_ROOT = Path('/home/kwung/deploy/bluegreen/runtime')
MAX_POLICY_BYTES = 16384
ENVIRONMENT = 'dev'
NGINX_LOCK = Path('/run/lock/ieum-bluegreen-nginx.lock')

def configure_environment(environment):
    """The root-installed filename selects the policy; caller environment is ignored."""
    global ENVIRONMENT, DESTINATION, FRONTEND_POLICY, FRONTEND_CANDIDATE, RUNTIME_ROOT
    if environment not in {'dev', 'main'}: raise ValueError('Invalid environment')
    ENVIRONMENT = environment
    suffix = '-main' if environment == 'main' else ''
    DESTINATION = Path(f'/etc/nginx/conf.d/ieum{suffix}-bluegreen.conf')
    FRONTEND_POLICY = Path(f'/etc/nginx/ieum{suffix}-frontend-deployment.json')
    home = Path('/home/kwung/deploy/bluegreen' + suffix)
    FRONTEND_CANDIDATE = home / 'frontend-deployment-candidate.json'
    RUNTIME_ROOT = home / 'runtime'

if Path(__file__).name == 'ieum-main-bluegreen-nginx':
    configure_environment('main')

@contextlib.contextmanager
def nginx_lock():
    descriptor = os.open(NGINX_LOCK, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077 or info.st_nlink != 1:
            raise ValueError('Invalid shared Nginx lock')
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)

def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Invalid frontend deployment settings')
        result[key] = value
    return result

def validate_split_settings(value):
    try:
        if not isinstance(value, dict) or set(value) != {'version', 'data', 'blue', 'green'} or type(value['version']) is not int or value['version'] != 2:
            raise ValueError()
        data = value['data']
        if not isinstance(data, dict) or set(data) != {'network', 'subnet', 'stores'} or \
                not isinstance(data['stores'], dict) or set(data['stores']) != {'postgres', 'redis', 'mongo'}:
            raise ValueError()
        networks, names, addresses = [], set(), set()
        reserved = {'ieum-' + app for app in ('frontend', 'backend', 'agent')} | \
            {'ieum-' + color + '-' + app for color in ('blue', 'green') for app in ('backend', 'agent')}
        store_names = list(data['stores'].values())
        for color in ('blue', 'green'):
            slot = value[color]
            if not isinstance(slot, dict) or set(slot) != {'network', 'subnet', 'containerName', 'ip', 'backendIp', 'agentIp', 'envDir', 'envFile'} or \
                    any(not isinstance(v, str) or any(ord(c) < 32 or c in '\x7f\x85\u2028\u2029' for c in v) for v in slot.values()):
                raise ValueError()
            name = slot['containerName']
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', name) or name in reserved or name in names:
                raise ValueError()
            names.add(name)
            directory, file = Path(slot['envDir']), Path(slot['envFile'])
            allowed = RUNTIME_ROOT / color
            if str(directory) != slot['envDir'] or '..' in directory.parts or \
                    (directory != allowed and allowed not in directory.parents) or \
                    file.parent != directory or str(file) != slot['envFile'] or file.name not in {'frontend.env', '.env'} or \
                    any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', part) for part in directory.relative_to(allowed).parts):
                raise ValueError()
        for slot in (value['blue'], value['green'], data):
            if not isinstance(slot['network'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,62}', slot['network']) or slot['network'] in {'host', 'none', 'bridge'}:
                raise ValueError()
            network = ipaddress.IPv4Network(slot['subnet'], strict=True)
            private_blocks = [ipaddress.IPv4Network(block) for block in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')]
            if str(network) != slot['subnet'] or not any(network.subnet_of(block) for block in private_blocks):
                raise ValueError()
            if any(slot['network'] == existing_name or network.overlaps(existing_net) for existing_name, existing_net in networks):
                raise ValueError()
            networks.append((slot['network'], network))
        prefix = 'ieum_main_net_' if ENVIRONMENT == 'main' else 'ieum_net_'
        if value['blue']['network'] != prefix + 'blue' or value['green']['network'] != prefix + 'green':
            raise ValueError()
        if ENVIRONMENT == 'main':
            if data['network'] != 'ieum_main_net_data' or data['stores'] != {
                    store: 'ieum-main-' + store for store in ('postgres', 'redis', 'mongo')}:
                raise ValueError()
            if any(value[color]['containerName'] != 'ieum-main-' + color + '-frontend' for color in ('blue', 'green')):
                raise ValueError()
        elif any(value[color]['containerName'].startswith('ieum-main-') for color in ('blue', 'green')) or \
                any(container.startswith('ieum-main-') for container in store_names):
            raise ValueError()
        for container in store_names:
            if not isinstance(container, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', container) or container in reserved | names:
                raise ValueError()
        if len(set(store_names)) != 3:
            raise ValueError()
        for color in ('blue', 'green'):
            slot = value[color]; network = ipaddress.IPv4Network(slot['subnet'])
            for key in ('ip', 'backendIp', 'agentIp'):
                address = ipaddress.IPv4Address(slot[key])
                if str(address) != slot[key] or address not in network or address in (network.network_address, network.broadcast_address) or \
                        slot[key] in addresses or slot[key] in ROUTES['legacy']:
                    raise ValueError()
                addresses.add(slot[key])
        return value
    except (AttributeError, TypeError, ValueError, KeyError):
        raise ValueError('Invalid separated-network deployment settings') from None


def validate_frontend_settings(value):
    """Only typed deployment data; schema 1 remains readable for old recovery."""
    if isinstance(value, dict) and value.get('version') == 2:
        return validate_split_settings(value)
    if ENVIRONMENT == 'main':
        raise ValueError('MAIN requires a separated-network policy')
    try:
        if not isinstance(value, dict) or set(value) != {'version', 'network', 'subnet', 'blue', 'green'} or \
                type(value['version']) is not int or value['version'] != 1:
            raise ValueError()
        if not isinstance(value['network'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,62}', value['network']) or \
                value['network'] in {'host', 'none', 'bridge'}:
            raise ValueError()
        network = ipaddress.IPv4Network(value['subnet'], strict=True)
        if str(network) != value['subnet']:
            raise ValueError()
        # The three apps share one network. Existing backend/agent addresses and
        # legacy rollback routes must remain reachable in that same subnet.
        other_ips = {address for route in ROUTES.values() for address in route}
        if any(ipaddress.IPv4Address(address) not in network for address in other_ips):
            raise ValueError()
        names, ips, directories, files = set(), set(), set(), set()
        reserved_names = {'ieum-' + app for app in ('frontend', 'backend', 'agent')} | \
            {'ieum-' + color + '-' + app for color in ('blue', 'green') for app in ('backend', 'agent')}
        for color in ('blue', 'green'):
            slot = value[color]
            if not isinstance(slot, dict) or set(slot) != {'containerName', 'ip', 'envDir', 'envFile'} or \
                    any(not isinstance(v, str) or any(ord(c) < 32 or c in '\x7f\x85\u2028\u2029' for c in v) for v in slot.values()):
                raise ValueError()
            name = slot['containerName']
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', name) or name in names or name in reserved_names:
                raise ValueError()
            address = ipaddress.IPv4Address(slot['ip'])
            if str(address) != slot['ip'] or address not in network or address in (network.network_address, network.broadcast_address) or \
                    slot['ip'] in ips or slot['ip'] in other_ips - {ROUTES['blue'][0], ROUTES['green'][0]}:
                raise ValueError()
            directory, file = Path(slot['envDir']), Path(slot['envFile'])
            # Keep user-controlled destinations out of controller state, source,
            # root configuration, and the other apps' runtime files.
            allowed = RUNTIME_ROOT / color
            if not directory.is_absolute() or str(directory) != slot['envDir'] or '..' in directory.parts or \
                    directory != allowed and allowed not in directory.parents or \
                    file.parent != directory or str(file) != slot['envFile'] or file.name not in {'frontend.env', '.env'} or \
                    directory in directories or file in files:
                raise ValueError()
            if any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', part)
                   for part in directory.relative_to(allowed).parts):
                raise ValueError()
            names.add(name); ips.add(slot['ip']); directories.add(directory); files.add(file)
        return value
    except (TypeError, ValueError, KeyError):
        raise ValueError('Invalid frontend deployment settings') from None

def read_frontend_file(path, *, owner, mode):
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != owner or \
                stat.S_IMODE(metadata.st_mode) != mode or metadata.st_nlink != 1 or metadata.st_size > MAX_POLICY_BYTES:
            raise ValueError()
        with os.fdopen(descriptor, 'rb', closefd=False) as source:
            raw = source.read(MAX_POLICY_BYTES + 1)
        if len(raw) > MAX_POLICY_BYTES:
            raise ValueError()
        return validate_frontend_settings(json.loads(raw, object_pairs_hook=unique_object))
    except (OSError, ValueError, TypeError, RecursionError):
        raise ValueError('Unable to validate frontend deployment settings') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)

def frontend_policy():
    if not os.path.lexists(FRONTEND_POLICY):
        if os.path.lexists(FRONTEND_CANDIDATE.parent / 'control/frontend-deployment-sha256'):
            raise ValueError('Pinned frontend policy is missing')
        return None
    return read_frontend_file(FRONTEND_POLICY, owner=0, mode=0o644)

def policy_text(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n'

def configure_frontend():
    value = read_frontend_file(FRONTEND_CANDIDATE, owner=pwd.getpwnam('kwung').pw_uid, mode=0o600)
    current = frontend_policy()
    if current is not None:
        if value != current:
            raise ValueError('Frontend deployment settings are pinned; plan a topology migration')
        return
    if not DESTINATION.exists() or not re.search(r'^# Managed by ieum-bluegreen-nginx\. active=legacy$', DESTINATION.read_text(), re.MULTILINE):
        raise ValueError('Configure frontend deployment settings before Blue/Green bootstrap')
    if value['version'] != 2:
        raise ValueError('New configuration requires separated-network settings')
    # This function is called as root under the controller's deployment lock.
    atomic_write(FRONTEND_POLICY, policy_text(value))
    os.chown(FRONTEND_POLICY, 0, 0)

def render(color):
    if color not in ROUTES:
        raise ValueError('Expected legacy, blue, or green')
    front, back, agent = ROUTES[color]
    policy = frontend_policy()
    if ENVIRONMENT == 'main' and color != 'legacy' and policy is None:
        raise ValueError('MAIN routing requires its pinned policy')
    if policy is not None and color in ('blue', 'green'):
        front = policy[color]['ip']
        if policy['version'] == 2:
            back, agent = policy[color]['backendIp'], policy[color]['agentIp']
    prefix = 'ieum_main' if ENVIRONMENT == 'main' else 'ieum'
    ports = (80, 8080, 8000)
    if ENVIRONMENT == 'main' and color == 'legacy':
        front = back = agent = '127.0.0.1'
        ports = (9, 9, 9)
    return f'''# Managed by ieum-bluegreen-nginx. active={color}
map $host ${prefix}_environment {{ default {color}; }}
map $http_upgrade ${prefix}_connection_upgrade {{ default upgrade; '' close; }}
upstream {prefix}_frontend {{ server {front}:{ports[0]}; }}
upstream {prefix}_backend {{ server {back}:{ports[1]}; }}
upstream {prefix}_agent {{ server {agent}:{ports[2]}; }}
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
        directory = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)

def validate_nginx():
    command = ['/usr/sbin/nginx', '-t']
    result = subprocess.run(command, check=True, capture_output=True)
    stderr = result.stderr.decode(errors='replace') if isinstance(result.stderr, bytes) else result.stderr
    # nginx accepts duplicate server names with exit status 0 and ignores one site.
    if ENVIRONMENT == 'main' and isinstance(stderr, str) and 'conflicting server name' in stderr.lower():
        raise subprocess.CalledProcessError(1, command, output=result.stdout, stderr=result.stderr)

def apply(color):
    previous = DESTINATION.read_text() if DESTINATION.exists() else None
    atomic_write(DESTINATION, render(color))
    try:
        validate_nginx()
        subprocess.run(['/usr/bin/systemctl', 'reload', 'nginx'], check=True, capture_output=True)
    except subprocess.CalledProcessError as error:
        if previous is not None:
            atomic_write(DESTINATION, previous)
            restored = subprocess.run(['/usr/bin/systemctl', 'reload', 'nginx'], capture_output=True)
            if restored.returncode:
                raise RuntimeError('Nginx change failed and restore reload failed; inspect the active route') from error
        else:
            DESTINATION.unlink(missing_ok=True)
        sys.stderr.write(error.stderr.decode(errors='replace') if isinstance(error.stderr, bytes) else str(error))
        raise

def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) == 2 and args[0] == 'validate-frontend':
        value = read_frontend_file(args[1], owner=os.geteuid(), mode=0o600)
        print(policy_text(value), end='')
    elif args == ['read-frontend']:
        print(policy_text(frontend_policy()), end='')
    elif os.geteuid() == 0 and args == ['configure-frontend']:
        with nginx_lock(): configure_frontend()
    elif os.geteuid() == 0 and len(args) == 1 and args[0] in ROUTES:
        with nginx_lock(): apply(args[0])
        print('Nginx active environment:', args[0])
    else:
        raise ValueError('Invalid Nginx helper operation')

if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError):
        sys.exit('Nginx helper refused invalid settings or operation')
