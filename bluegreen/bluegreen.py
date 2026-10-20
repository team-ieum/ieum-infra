#!/usr/bin/env python3
"""IEUM blue/green deployment. Secrets stay in owner-only files on the server."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HOME = Path(os.environ.get('IEUM_BLUEGREEN_HOME', '/home/kwung/deploy/bluegreen'))
APPS = ('frontend', 'agent', 'backend')
IPS = {'blue': {'frontend': '10.10.10.20', 'backend': '10.10.10.21', 'agent': '10.10.10.24'},
       'green': {'frontend': '10.10.10.30', 'backend': '10.10.10.31', 'agent': '10.10.10.34'}}
PORTS = {'frontend': 80, 'agent': 8000, 'backend': 8080}
HEALTH = {'frontend': '/', 'agent': '/health', 'backend': '/actuator/health/readiness'}
LEGACY = {a: 'ieum-' + a for a in APPS}
ROUTE_CONFIG = Path('/etc/nginx/conf.d/ieum-bluegreen.conf')
ASSETS = Path('/var/lib/ieum-bluegreen/assets')
JOB_DRAIN_TIMEOUT = 660
CONNECTION_GRACE = 30
STOP_TIMEOUT = 1350  # web + workflow executor + relay shutdown phases, with headroom
PROTOCOL_VERSION = 2

def run(*args):
    result = subprocess.run(args, text=True, capture_output=True)
    if result.returncode:
        # Docker may include env-file contents in some parse errors; never echo arguments or raw stderr.
        raise RuntimeError(f'{args[0]} operation failed (exit {result.returncode})')
    return result.stdout.strip()

def docker(*args):
    return run('docker', *args)

def inspect(name):
    return json.loads(docker('inspect', name))[0]

def exists(name):
    return subprocess.run(['docker', 'inspect', name], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0

def name(color, app):
    return LEGACY[app] if color == 'legacy' else f'ieum-{color}-{app}'

def atomic(path, value, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(temp): os.unlink(temp)

def state():
    data = json.loads((HOME / 'state.json').read_text())
    # Read the original format, but give rollback an explicit release identity.
    for color, slot in list(data.get('slots', {}).items()):
        if 'images' not in slot:
            data['slots'][color] = {
                'status': 'active' if color == data['active'] else
                          'retained' if color == data.get('previous') else 'candidate',
                'images': slot, 'release': release_id(slot)}
    data['version'] = 2
    return data

def release_id(images):
    return hashlib.sha256(json.dumps(images, sort_keys=True).encode()).hexdigest()[:16]

def save(data):
    atomic(HOME / 'state.json', json.dumps(data, indent=2) + '\n')

def record(action, **details):
    with (HOME / 'history.jsonl').open('a') as output:
        output.write(json.dumps({'at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                                 'action': action, **details}) + '\n')
        output.flush()
        os.fsync(output.fileno())

def route_color():
    match = re.search(r'^# Managed by ieum-bluegreen-nginx\. active=(legacy|blue|green)$',
                      ROUTE_CONFIG.read_text(), re.MULTILINE)
    if not match:
        raise RuntimeError('Cannot determine the installed Nginx route')
    return match.group(1)

def assert_consistent(data=None):
    data = state() if data is None else data
    if route_color() != data['active'] or \
            (HOME / 'control/active').read_text().strip() != data['active']:
        raise RuntimeError('Nginx route, ownership and saved state disagree; inspect/recover before changing containers')
    if (HOME / 'control/phase').read_text().strip() == 'ready':
        for container in LEGACY.values():
            if exists(container) and inspect(container)['State']['Status'] == 'running':
                raise RuntimeError('Legacy application is running alongside Blue/Green; stop the old deployment workflow first')

def slot_images(color):
    return {app: inspect(name(color, app))['Image'] for app in APPS}

def verify_slot(color, allowed):
    slot = state().get('slots', {}).get(color, {})
    if slot.get('status') not in allowed or set(slot.get('images', {})) != set(APPS):
        raise RuntimeError('Environment is not an eligible retained/candidate release')
    if slot_images(color) != slot['images']:
        raise RuntimeError('Container images differ from the recorded release')
    return slot

def invalidate_slot(color, status='preparing'):
    data = state()
    if data.get('previous') == color:
        data['previous'] = None
    data.setdefault('slots', {}).setdefault(color, {})['status'] = status
    save(data)

def archive_frontend(color):
    """Keep immutable assets independently of either reusable application slot."""
    if not exists(name(color, 'frontend')):
        return
    ASSETS.mkdir(parents=True, exist_ok=True, mode=0o755)
    os.chmod(ASSETS, 0o755)  # Workflows intentionally run with umask 077 for secret env files.
    staging_root = ASSETS.parent / 'staging'
    staging_root.mkdir(exist_ok=True, mode=0o700)
    staging = Path(tempfile.mkdtemp(prefix='.assets-', dir=staging_root))
    try:
        docker('cp', name(color, 'frontend') + ':/usr/share/nginx/html/assets/.', str(staging))
        for source in sorted(staging.rglob('*')):
            if source.is_symlink() or not (source.is_dir() or source.is_file()):
                raise RuntimeError('Frontend asset archive must contain only regular files/directories')
            if not source.is_file():
                continue
            target = ASSETS / source.relative_to(staging)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            for directory in (target.parent, *target.parent.parents):
                if directory == ASSETS.parent: break
                os.chmod(directory, 0o755)
            if target.exists():
                if target.is_symlink() or hashlib.sha256(target.read_bytes()).digest() != \
                        hashlib.sha256(source.read_bytes()).digest():
                    raise RuntimeError('An archived asset URL was reused with different contents; use immutable hashed asset names')
                continue
            # Staging and archive are on the same filesystem; a request sees a whole file.
            os.chmod(source, 0o644)
            os.replace(source, target)
        record('assets_archived', environment=color)
    finally:
        shutil.rmtree(staging)

def request(url, body=None, timeout=5):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={'Content-Type': 'application/json'} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.status, response.read()

def base(color, app):
    return f'http://{IPS[color][app]}:{PORTS[app]}'

def control(color, action=None):
    body = None if action is None else {'action': action, 'token': (HOME / 'control/token').read_text().strip()}
    return json.loads(request(base(color, 'backend') + '/actuator/deployment', body,
                              timeout=30 if action else 5)[1])

def check(color, timeout=120):
    deadline = time.monotonic() + timeout
    pending = set(APPS)
    while pending and time.monotonic() < deadline:
        for app in tuple(pending):
            try:
                if inspect(name(color, app))['State']['Status'] != 'running': continue
                code, body = request(base(color, app) + HEALTH[app])
                if code != 200: continue
                if app == 'backend' and json.loads(body).get('status') != 'UP': continue
                pending.remove(app)
            except (RuntimeError, OSError, ValueError):
                pass
        if pending: time.sleep(2)
    if pending:
        raise RuntimeError('Candidate health checks failed: ' + ', '.join(sorted(pending)))
    details = control(color)
    if not details.get('clusteredQuartz'):
        raise RuntimeError('Candidate backend is not using clustered Quartz')
    if details.get('protocolVersion') != PROTOCOL_VERSION:
        raise RuntimeError('Backend deployment protocol is outdated; deploy the corrected backend before other services')
    env = dict(e.split('=', 1) for e in inspect(name(color, 'backend'))['Config']['Env'])
    if env.get('AGENT_SERVICE_URL') != base(color, 'agent'):
        raise RuntimeError('Backend agent URL does not match its environment')
    return details

def init():
    if (HOME / 'state.json').exists():
        print('Already initialized'); return
    snapshots = {}
    for app in APPS:
        info = inspect(LEGACY[app])
        image = json.loads(docker('image', 'inspect', info['Image']))[0]
        defaults = dict(e.split('=', 1) for e in image['Config'].get('Env', []))
        runtime = {k: v for k, v in (e.split('=', 1) for e in info['Config'].get('Env', []))
                   if defaults.get(k) != v}
        snapshots[app] = {'image': info['Image'], 'environment': runtime}
    atomic(HOME / 'snapshots/legacy.json', json.dumps(snapshots, indent=2))
    atomic(HOME / 'control/active', 'legacy\n', 0o644)
    atomic(HOME / 'control/phase', 'legacy\n', 0o644)
    atomic(HOME / 'control/token', secrets.token_urlsafe(48) + '\n')
    save({'version': 1, 'active': 'legacy', 'previous': None, 'slots': {}})
    record('initialized', active='legacy')
    print('Initialized; legacy services are unchanged')

def drain(color, timeout=JOB_DRAIN_TIMEOUT, connection_grace=CONNECTION_GRACE):
    if not exists(name(color, 'backend')): return
    if inspect(name(color, 'backend'))['State']['Status'] != 'running': return
    # An inactive release stays drained even if replacement fails. Resuming it
    # would let the previous code consume work created by the new release.
    control(color, 'drain')
    deadline = time.monotonic() + timeout
    while True:
        details = control(color)
        if not any(details.get(k, 0) for k in ('activeJobs', 'queuedJobs', 'activeRequests')):
            break
        if time.monotonic() >= deadline:
            raise RuntimeError('Previous environment still has work; retained in drain mode without force-stop')
        time.sleep(1)
    deadline = time.monotonic() + connection_grace
    while any(details.get(k, 0) for k in ('sseSubscribers', 'websocketUsers')):
        if time.monotonic() >= deadline:
            if details.get('protocolVersion') != PROTOCOL_VERSION:
                raise RuntimeError('Old backend cannot retire connections; wait for clients before the initial upgrade')
            control(color, 'retire')
            break
        time.sleep(1)
        details = control(color)
    else:
        if details.get('protocolVersion') == PROTOCOL_VERSION:
            control(color, 'retire')
    # Retire rejects new HTTP work and closes existing realtime sessions. Check
    # again because a request may have entered during the grace interval.
    deadline = time.monotonic() + timeout
    while True:
        details = control(color)
        if not any(details.get(k, 0) for k in
                   ('activeJobs', 'queuedJobs', 'activeRequests', 'sseSubscribers', 'websocketUsers')):
            return
        if time.monotonic() >= deadline:
            raise RuntimeError('Retired environment is not quiescent; preserved without force-stop')
        time.sleep(1)

def remove_inactive(color):
    data = state()
    assert_consistent(data)
    if data['active'] == color:
        raise RuntimeError('Refusing to replace the active environment')
    # Validate all ownership labels before touching any application or release metadata.
    for app in APPS:
        container = name(color, app)
        if exists(container) and inspect(container)['Config'].get('Labels', {}).get('ieum.bluegreen.managed') != 'true':
            raise RuntimeError('Refusing to remove unmanaged container: ' + container)
    drain(color)
    if data.get('slots', {}).get(color, {}).get('status') == 'retained':
        archive_frontend(color)
    # Invalidate rollback before removing the first container, including crash paths.
    invalidate_slot(color)
    for app in reversed(APPS):
        container = name(color, app)
        if exists(container):
            info = inspect(container)
            if info['State']['Status'] == 'running': docker('stop', '--time', str(STOP_TIMEOUT), container)
            docker('rm', container)

def read_env(path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
        raise RuntimeError('Environment candidate must be a private regular file owned by the deployment account')
    if info.st_size > 1024 * 1024:
        raise RuntimeError('Environment candidate is too large')
    values = {}
    for line in path.read_text().splitlines():
        if not line or line.startswith('#'): continue
        key, separator, value = line.partition('=')
        if not separator or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key) or '\x00' in value:
            raise RuntimeError('Invalid environment candidate format')
        if key in values:
            raise RuntimeError('Duplicate environment key')
        values[key] = value
    return values

def env_values(app, snapshot, active, override=None):
    # Reuse the deployed configuration, not mutable shared .env files of other apps.
    if active == 'legacy':
        values = dict(snapshot['environment'])
    else:
        path = HOME / 'runtime' / active / (app + '.env')
        if path.exists():
            values = read_env(path)
        else:
            deployed = inspect(name(active, app))
            image = json.loads(docker('image', 'inspect', deployed['Image']))[0]
            defaults = dict(e.split('=', 1) for e in image['Config'].get('Env', []))
            values = {k: v for k, v in (e.split('=', 1) for e in deployed['Config'].get('Env', []))
                      if defaults.get(k) != v}
    if override is not None:
        values.update(read_env(override))
    return values

def prepare(color, overrides, env_overrides=None):
    data = state()
    assert_consistent(data)
    if color == data['active']: raise RuntimeError('Target environment is already active')
    env_overrides = {} if env_overrides is None else env_overrides
    if set(env_overrides) - set(overrides):
        raise RuntimeError('Environment changes require an explicit image deployment of the same service')
    snapshots = json.loads((HOME / 'snapshots/legacy.json').read_text())
    if data['active'] != 'legacy':
        for app in APPS:
            snapshots[app]['image'] = inspect(name(data['active'], app))['Image']
    images = {app: json.loads(docker('image', 'inspect', overrides.get(app, snapshots[app]['image'])))[0]
              for app in APPS}
    if images['backend']['Config'].get('Labels', {}).get('ieum.bluegreen.capable') != 'true':
        raise RuntimeError('Backend image lacks distributed deployment support')
    if images['backend']['Config'].get('Labels', {}).get('ieum.bluegreen.protocol') != str(PROTOCOL_VERSION):
        raise RuntimeError('Deploy the corrected protocol-2 backend before frontend/agent deployments')
    environments = {app: env_values(app, snapshots[app], data['active'], env_overrides.get(app)) for app in APPS}
    remove_inactive(color)
    data = state()  # remove_inactive invalidates the former rollback release.
    image_ids = {app: images[app]['Id'] for app in APPS}
    data['slots'][color] = {'status': 'preparing', 'images': image_ids, 'release': release_id(image_ids)}
    save(data)
    created = []
    try:
        for app in APPS:
            env = environments[app]
            if app == 'agent':
                env['TINI_KILL_PROCESS_GROUP'] = '1'
            if app == 'backend':
                env.pop('IEUM_WORKFLOW_QUEUE_CONSUMER_NAME', None)
                env.setdefault('DB_POOL_MAX_SIZE', '35')
                env.setdefault('DB_POOL_MIN_IDLE', '5')
                env.update({'AGENT_SERVICE_URL': base(color, 'agent'),
                    'IEUM_DEPLOYMENT_DISTRIBUTED_ENABLED': 'true', 'IEUM_DEPLOYMENT_INSTANCE': color,
                    'IEUM_DEPLOYMENT_START_DRAINED': 'true', 'IEUM_SCHEDULER_JOB_STORE_TYPE': 'jdbc',
                    'IEUM_SCHEDULER_AUTO_STARTUP': 'false', 'SPRING_JPA_HIBERNATE_DDL_AUTO': 'validate',
                    'SERVER_SHUTDOWN': 'graceful', 'SPRING_LIFECYCLE_TIMEOUT_PER_SHUTDOWN_PHASE': '600s',
                    'MANAGEMENT_ENDPOINTS_WEB_EXPOSURE_INCLUDE': 'health,deployment'})
            envfile = HOME / 'runtime' / color / (app + '.env')
            if any('\n' in v or '\r' in v for v in env.values()):
                raise RuntimeError('Multiline environment values require an explicit deployment configuration')
            atomic(envfile, ''.join(k + '=' + v + '\n' for k, v in env.items()))
            args = ['create', '--init', '--name', name(color, app), '--restart', 'unless-stopped',
                    '--network', 'ieum_net_green', '--ip', IPS[color][app],
                    '--label', 'ieum.bluegreen.managed=true', '--label', 'ieum.bluegreen.color=' + color,
                    '--log-opt', 'max-size=10m', '--log-opt', 'max-file=5',
                    '--stop-timeout', str(STOP_TIMEOUT), '--env-file', str(envfile)]
            if app == 'backend': args += ['--mount', f'type=bind,src={HOME}/control,dst=/deployment,readonly']
            docker(*args, images[app]['Id'])
            created.append(name(color, app))
            docker('start', name(color, app))
        details = check(color)
        if not details.get('draining') or not details.get('schedulerStandby'):
            raise RuntimeError('Candidate background workers started before activation')
        data['slots'][color]['status'] = 'candidate'
        save(data)
        record('prepared', target=color, images=data['slots'][color])
        print('Prepared and healthy:', color)
    except Exception:
        for container in reversed(created):
            with contextlib.suppress(Exception): docker('stop', '--time', str(STOP_TIMEOUT), container)
            with contextlib.suppress(Exception): docker('rm', container)
        invalidate_slot(color, 'failed')
        raise

def route(color):
    run('sudo', '-n', '/usr/local/sbin/ieum-bluegreen-nginx', color)

def verify_ingress(color, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(['curl', '--fail', '--silent', '--show-error', '--max-time', '5',
            '--resolve', 'green.kiacms.co.kr:11111:127.0.0.1', '-D', '-', '-o', '/dev/null',
            'https://green.kiacms.co.kr:11111/'], capture_output=True, text=True)
        if result.returncode == 0 and ('x-ieum-environment: ' + color) in result.stdout.lower():
            try:
                for path in ('/v3/api-docs', '/openapi.json'):
                    probe = subprocess.run(['curl', '--fail', '--silent', '--show-error', '--max-time', '5',
                        '--resolve', 'green.kiacms.co.kr:11111:127.0.0.1', '-D', '-', '-o', '/dev/null',
                        'https://green.kiacms.co.kr:11111' + path], capture_output=True, text=True)
                    if probe.returncode or ('x-ieum-environment: ' + color) not in probe.stdout.lower():
                        raise RuntimeError('Ingress route not ready')
                return
            except RuntimeError:
                pass
        # reload acknowledges before the new workers necessarily accept their first connection.
        time.sleep(0.25)
    raise RuntimeError('TLS ingress verification failed after switching')


def switch(color):
    data = state()
    assert_consistent(data)
    old = data['active']
    original_data = json.loads(json.dumps(data))
    if old == color: print('Already active:', color); return
    target_slot = verify_slot(color, {'candidate', 'retained'})
    check(color)
    archive_frontend(old)
    archive_frontend(color)  # Reject reused asset URLs before any traffic/worker change.
    transaction = {'from': old, 'to': color, 'started': time.time(), 'stage': 'started',
                   'snapshot': original_data, 'targetImages': target_slot['images']}
    atomic(HOME / 'transaction.json', json.dumps(transaction))
    try:
        if old in IPS:
            control(old, 'drain')
        control(color, 'resume')
        route(color)
        # Probe the same TLS virtual host, independently of external pfSense NAT.
        verify_ingress(color)
        data['active'], data['previous'] = color, old
        if old in IPS:
            old_images = slot_images(old)
            data['slots'][old] = {'status': 'retained', 'images': old_images, 'release': release_id(old_images)}
        else:
            data['previous'] = None
        data['slots'][color]['status'] = 'active'
        atomic(HOME / 'control/active', color + '\n', 0o644)
        save(data)
        transaction['stage'] = 'committed'
        atomic(HOME / 'transaction.json', json.dumps(transaction))
        record('switched', previous=old, active=color)
        (HOME / 'transaction.json').unlink(missing_ok=True)
        print(f'Traffic switched: {old} -> {color}; previous environment retained')
    except Exception as original_error:
        try:
            restore_previous(transaction)
        except Exception as recovery_error:
            # Preserve the original cause and durable journal for a retry. Do
            # not claim rollback success when Nginx/ownership could disagree.
            with contextlib.suppress(Exception):
                record('switch_failed_recovery_required', previous=old, candidate=color,
                       originalError=type(original_error).__name__, recoveryError=type(recovery_error).__name__)
            raise RuntimeError('Switch failed and rollback is incomplete; transaction retained for recover') from original_error
        raise

def restore_previous(transaction):
    old, candidate = transaction['from'], transaction['to']
    original = transaction.get('snapshot')
    if original is None:
        raise RuntimeError('Old-format transaction requires manual inspection before recovery')
    route(old)
    verify_ingress(old)
    control(candidate, 'drain')
    atomic(HOME / 'control/active', old + '\n', 0o644)
    if original.get('previous') == candidate:
        original['previous'] = None
    original.get('slots', {}).get(candidate, {})['status'] = 'failed'
    save(original)
    if old in IPS:
        control(old, 'resume')
    record('switch_failed_rolled_back', active=old, candidate=candidate)
    (HOME / 'transaction.json').unlink(missing_ok=True)

def recover():
    transaction_path = HOME / 'transaction.json'
    if not transaction_path.exists():
        print('No unfinished transaction'); return
    transaction = json.loads(transaction_path.read_text())
    target = transaction['to']
    data = state()
    # A process may die after state commit but before unlink. Finalize the
    # committed target instead of undoing a successful deployment.
    committed = (data['active'] == target and route_color() == target and
                 (HOME / 'control/active').read_text().strip() == target)
    if committed:
        if slot_images(target) != transaction.get('targetImages'):
            raise RuntimeError('Committed release images changed; manual inspection required')
        check(target)
        verify_ingress(target)
        if transaction['from'] in IPS:
            control(transaction['from'], 'drain')
        control(target, 'resume')
        record('recovered_committed_switch', active=target)
        transaction_path.unlink()
        print('Finalized completed switch:', target)
    else:
        restore_previous(transaction)
        print('Recovered previous environment:', transaction['from'])

def rollback(expected_release):
    data = state()
    assert_consistent(data)
    previous = data.get('previous')
    if previous not in IPS:
        raise RuntimeError('No retained rollback release is available')
    slot = verify_slot(previous, {'retained'})
    if not expected_release or expected_release != slot.get('release'):
        raise RuntimeError('Rollback requires the exact retained release ID shown by status')
    print('Rollback release:', slot['release'], json.dumps(slot['images'], sort_keys=True))
    switch(previous)

def pg(sql):
    return run('docker', 'exec', 'ieum-postgres', 'sh', '-c',
        'exec psql -X -A -t -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"', 'sh', sql)

def finish_bootstrap():
    data = state()
    if data['active'] == 'legacy': raise RuntimeError('Switch to a healthy enhanced environment first')
    if (HOME / 'control/phase').read_text().strip() == 'ready':
        print('Already migrated'); return
    # The legacy application has in-memory events and a fixed queue consumer. Never overlap its
    # background workers with distributed workers. Wait for its requests and durable jobs to finish.
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        jobs = int(pg("select count(*) from workflow_runs where status in ('PENDING','RUNNING')"))
        queue = int(docker('exec', 'ieum-redis', 'redis-cli', '--raw', 'XLEN', 'ieum:exec:jobs'))
        active_connections = 0
        for app, port in (('backend', 8080), ('agent', 8000)):
            if not exists(LEGACY[app]) or inspect(LEGACY[app])['State']['Status'] != 'running': continue
            sockets = docker('exec', LEGACY[app], 'cat', '/proc/net/tcp', '/proc/net/tcp6')
            active_connections += sum(1 for line in sockets.splitlines()
                if len(line.split()) > 3 and line.split()[3] == '01'
                and line.split()[1].endswith(f':{port:04X}'))
        if jobs == 0 and queue == 0 and active_connections == 0: break
        time.sleep(2)
    else:
        raise RuntimeError('Legacy jobs/connections are still active; preserved. Retry finish-bootstrap later.')
    for app in ('backend', 'agent', 'frontend'):
        if exists(LEGACY[app]) and inspect(LEGACY[app])['State']['Status'] == 'running':
            docker('stop', '--time', str(STOP_TIMEOUT), LEGACY[app])
    docker('exec', 'ieum-redis', 'redis-cli', 'SET', 'ieum:deployment:mode', 'distributed-v1')
    atomic(HOME / 'control/phase', 'ready\n', 0o644)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if not control(data['active'])['schedulerStandby']: break
        time.sleep(2)
    else: raise RuntimeError('Distributed scheduler did not become ready')
    record('legacy_migrated', active=data['active'])
    print('Legacy drained; distributed workers and clustered scheduling enabled')

def status():
    data = state()
    output = {'active': data['active'], 'previous': data['previous'],
              'phase': (HOME / 'control/phase').read_text().strip(), 'environments': {},
              'slots': data.get('slots', {})}
    try:
        output['nginxRoute'] = route_color()
        output['routeConsistent'] = output['nginxRoute'] == data['active'] == (HOME / 'control/active').read_text().strip()
    except Exception:
        output['routeConsistent'] = False
    for color in ('blue', 'green'):
        services = {}
        for app in APPS:
            container = name(color, app)
            if exists(container):
                info = inspect(container)
                services[app] = {'state': info['State']['Status'], 'ip': IPS[color][app], 'image': info['Image']}
        output['environments'][color] = services
        with contextlib.suppress(Exception): output['environments'][color]['deployment'] = control(color)
    output['unfinishedTransaction'] = (HOME / 'transaction.json').exists()
    print(json.dumps(output, indent=2))

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('init'); sub.add_parser('status'); sub.add_parser('finish-bootstrap')
    for verb in ('prepare', 'switch', 'check'):
        p = sub.add_parser(verb); p.add_argument('color', choices=tuple(IPS))
        if verb == 'prepare':
            for app in APPS: p.add_argument('--' + app + '-image')
    p = sub.add_parser('rollback'); p.add_argument('--release', required=True)
    sub.add_parser('recover')
    p = sub.add_parser('deploy'); p.add_argument('--service', choices=APPS, required=True)
    p.add_argument('--image', required=True)
    p.add_argument('--env-file')
    args = parser.parse_args()
    HOME.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.command == 'status': status(); return
    with (HOME / 'deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (HOME / 'transaction.json').exists() and args.command != 'recover':
            raise RuntimeError('Interrupted switch detected; run recover or inspect status first')
        if args.command == 'recover':
            recover()
        elif args.command == 'init': init()
        elif args.command == 'prepare':
            prepare(args.color, {a: getattr(args, a + '_image') for a in APPS if getattr(args, a + '_image')})
        elif args.command == 'switch': switch(args.color)
        elif args.command == 'check': print(json.dumps(check(args.color), indent=2))
        elif args.command == 'finish-bootstrap': finish_bootstrap()
        elif args.command == 'rollback':
            rollback(args.release)
        elif args.command == 'deploy':
            active = state()['active']
            if active not in IPS: raise RuntimeError('Finish bootstrap before automatic deployments')
            target = 'green' if active == 'blue' else 'blue'
            prepare(target, {args.service: args.image}, {args.service: args.env_file} if args.env_file else None)
            switch(target)

if __name__ == '__main__':
    try: main()
    except Exception as error:
        print(f'Deployment stopped safely: {error}', file=sys.stderr)
        sys.exit(1)
