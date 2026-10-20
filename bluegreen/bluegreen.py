#!/usr/bin/env python3
"""IEUM blue/green deployment. Secrets stay in owner-only files on the server."""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import secrets
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
    finally:
        if os.path.exists(temp): os.unlink(temp)

def state():
    return json.loads((HOME / 'state.json').read_text())

def save(data):
    atomic(HOME / 'state.json', json.dumps(data, indent=2) + '\n')

def record(action, **details):
    with (HOME / 'history.jsonl').open('a') as output:
        output.write(json.dumps({'at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                                 'action': action, **details}) + '\n')

def request(url, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={'Content-Type': 'application/json'} if data else {})
    with urllib.request.urlopen(req, timeout=5) as response:
        return response.status, response.read()

def base(color, app):
    return f'http://{IPS[color][app]}:{PORTS[app]}'

def control(color, action=None):
    body = None if action is None else {'action': action, 'token': (HOME / 'control/token').read_text().strip()}
    return json.loads(request(base(color, 'backend') + '/actuator/deployment', body)[1])

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

def drain(color, timeout=120):
    if not exists(name(color, 'backend')): return
    if inspect(name(color, 'backend'))['State']['Status'] != 'running': return
    try:
        control(color, 'drain')
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            details = control(color)
            if not any(details[k] for k in ('activeJobs', 'queuedJobs', 'sseSubscribers', 'websocketUsers')):
                return
            time.sleep(2)
        raise RuntimeError('Previous environment still has jobs or live connections; preserved without force-stop')
    except Exception:
        with contextlib.suppress(Exception): control(color, 'resume')
        raise

def remove_inactive(color):
    if state()['active'] == color:
        raise RuntimeError('Refusing to replace the active environment')
    drain(color)
    for app in reversed(APPS):
        container = name(color, app)
        if exists(container):
            info = inspect(container)
            if info['Config'].get('Labels', {}).get('ieum.bluegreen.managed') != 'true':
                raise RuntimeError('Refusing to remove unmanaged container: ' + container)
            if info['State']['Status'] == 'running': docker('stop', '--time', '650', container)
            docker('rm', container)

def env_values(app, snapshot):
    values = dict(snapshot['environment'])
    path = Path('/home/kwung/deploy') / ('ieum-' + app) / '.env'
    if path.exists():
        for line in path.read_text().splitlines():
            if line and not line.lstrip().startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                values[key] = value
    return values

def prepare(color, overrides):
    data = state()
    if color == data['active']: raise RuntimeError('Target environment is already active')
    snapshots = json.loads((HOME / 'snapshots/legacy.json').read_text())
    if data['active'] != 'legacy':
        for app in APPS:
            snapshots[app]['image'] = inspect(name(data['active'], app))['Image']
    images = {app: json.loads(docker('image', 'inspect', overrides.get(app, snapshots[app]['image'])))[0]
              for app in APPS}
    if images['backend']['Config'].get('Labels', {}).get('ieum.bluegreen.capable') != 'true':
        raise RuntimeError('Backend image lacks distributed deployment support')
    remove_inactive(color)
    created = []
    try:
        for app in APPS:
            env = env_values(app, snapshots[app])
            if app == 'agent':
                env['TINI_KILL_PROCESS_GROUP'] = '1'
            if app == 'backend':
                env.setdefault('DB_POOL_MAX_SIZE', '35')
                env.setdefault('DB_POOL_MIN_IDLE', '5')
                env.update({'AGENT_SERVICE_URL': base(color, 'agent'),
                    'IEUM_DEPLOYMENT_DISTRIBUTED_ENABLED': 'true', 'IEUM_DEPLOYMENT_INSTANCE': color,
                    'IEUM_WORKFLOW_QUEUE_CONSUMER_NAME': color, 'IEUM_SCHEDULER_JOB_STORE_TYPE': 'jdbc',
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
                    '--stop-timeout', '650', '--env-file', str(envfile)]
            if app == 'backend': args += ['--mount', f'type=bind,src={HOME}/control,dst=/deployment,readonly']
            docker(*args, images[app]['Id'])
            created.append(name(color, app))
            docker('start', name(color, app))
        check(color)
        data['slots'][color] = {app: images[app]['Id'] for app in APPS}
        save(data)
        record('prepared', target=color, images=data['slots'][color])
        print('Prepared and healthy:', color)
    except Exception:
        for container in reversed(created):
            with contextlib.suppress(Exception): docker('stop', '--time', '650', container)
            with contextlib.suppress(Exception): docker('rm', container)
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
    old = data['active']
    original_data = json.loads(json.dumps(data))
    if old == color: print('Already active:', color); return
    check(color)
    control(color, 'resume')
    atomic(HOME / 'transaction.json', json.dumps({'from': old, 'to': color, 'started': time.time()}))
    try:
        route(color)
        # Probe the same TLS virtual host, independently of external pfSense NAT.
        verify_ingress(color)
        data['active'], data['previous'] = color, old
        atomic(HOME / 'control/active', color + '\n', 0o644)
        save(data)
        record('switched', previous=old, active=color)
        (HOME / 'transaction.json').unlink(missing_ok=True)
        print(f'Traffic switched: {old} -> {color}; previous environment retained')
    except Exception:
        route(old)
        atomic(HOME / 'control/active', old + '\n', 0o644)
        save(original_data)
        record('switch_failed_rolled_back', active=old, candidate=color)
        (HOME / 'transaction.json').unlink(missing_ok=True)
        raise

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
            docker('stop', '--time', '650', LEGACY[app])
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
              'phase': (HOME / 'control/phase').read_text().strip(), 'environments': {}}
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
    sub.add_parser('rollback'); sub.add_parser('recover')
    p = sub.add_parser('deploy'); p.add_argument('--service', choices=APPS, required=True)
    p.add_argument('--image', required=True)
    args = parser.parse_args()
    HOME.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.command == 'status': status(); return
    with (HOME / 'deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (HOME / 'transaction.json').exists() and args.command != 'recover':
            raise RuntimeError('Interrupted switch detected; run recover or inspect status first')
        if args.command == 'recover':
            transaction = json.loads((HOME / 'transaction.json').read_text())
            target = transaction['from']
            if target not in IPS:
                raise RuntimeError('Legacy bootstrap needs manual inspection before recovery')
            check(target)
            control(target, 'resume')
            route(target)
            verify_ingress(target)
            data = state()
            data['active'] = target
            data['previous'] = transaction['to']
            atomic(HOME / 'control/active', target + '\n', 0o644)
            save(data)
            record('recovered', active=target)
            (HOME / 'transaction.json').unlink()
            print('Recovered previous environment:', target)
        elif args.command == 'init': init()
        elif args.command == 'prepare':
            prepare(args.color, {a: getattr(args, a + '_image') for a in APPS if getattr(args, a + '_image')})
        elif args.command == 'switch': switch(args.color)
        elif args.command == 'check': print(json.dumps(check(args.color), indent=2))
        elif args.command == 'finish-bootstrap': finish_bootstrap()
        elif args.command == 'rollback':
            previous = state()['previous']
            if previous not in IPS: raise RuntimeError('No enhanced rollback environment is available')
            switch(previous)
        elif args.command == 'deploy':
            active = state()['active']
            if active not in IPS: raise RuntimeError('Finish bootstrap before automatic deployments')
            target = 'green' if active == 'blue' else 'blue'
            prepare(target, {args.service: args.image})
            switch(target)

if __name__ == '__main__':
    try: main()
    except Exception as error:
        print(f'Deployment stopped safely: {error}', file=sys.stderr)
        sys.exit(1)
