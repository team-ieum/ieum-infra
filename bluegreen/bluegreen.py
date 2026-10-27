#!/usr/bin/env python3
"""IEUM blue/green deployment. Secrets stay in owner-only files on the server."""
import argparse
import contextlib
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones
import errno
import fcntl
import hashlib
import json
import math
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
CANDIDATE_STOP_TIMEOUT = 30  # New, never-admitted candidates do not need the active workload drain.
CANDIDATE_CLEANUP_TIMEOUT = 240  # One aggregate budget, including Docker command timeouts.
CANDIDATE_DOCKER_TIMEOUT = 10
ISOLATION_TIMEOUT = 30  # Explicit force may interrupt work; never use this for normal drain.
PROTOCOL_VERSION = 2
DEFAULT_OPERATION_LOCK_TIMEOUT = 600
DEFAULT_DEPLOY_LOCK_TIMEOUT = 14400
MAX_LOCK_TIMEOUT = 14400
DEPLOYMENT_RESERVE_SECONDS = 9000  # Normal phases, or failed preparation plus bounded candidate cleanup, with headroom.
SWITCH_RESERVE_SECONDS = 1800  # Includes the 660s drain, connection grace, checks and control requests.
DEFAULT_LEGACY_PROBE_TIMEOUT = 10
DEFAULT_LEGACY_PROBE_ATTEMPTS = 2
LEGACY_PROBE_FAILURE_CODES = frozenset({'probe_timeout', 'probe_transport_failure', 'probe_command_failed',
    'probe_invalid_result', 'probe_incomplete_or_changed_process'})
BACKEND_TIME_ZONES = frozenset(available_timezones()) - {'localtime'}
LEGACY_ZONE_HELPER_SHA256 = 'f5cb8a675a43993b9fd16a503a64ecc04287fd3fef1b9d0baaf70f4c76f04341'

def default_lock_timeout(command):
    return DEFAULT_DEPLOY_LOCK_TIMEOUT if command == 'deploy' else DEFAULT_OPERATION_LOCK_TIMEOUT

def lock_timeout(value):
    try:
        seconds = float(value)
        if not math.isfinite(seconds) or not 0 <= seconds <= MAX_LOCK_TIMEOUT:
            raise ValueError()
        return seconds
    except ValueError as failure:
        raise argparse.ArgumentTypeError(f'Lock timeout must be between 0 and {MAX_LOCK_TIMEOUT} seconds') from failure

def job_deadline(value):
    try:
        deadline = float(value)
        if not math.isfinite(deadline) or deadline <= 0:
            raise ValueError()
        return deadline
    except ValueError as failure:
        raise argparse.ArgumentTypeError('Job deadline must be a finite positive Unix timestamp') from failure

def bounded_legacy_probe_integer(value, minimum, maximum, setting):
    try:
        if isinstance(value, bool) or not re.fullmatch(r'[0-9]+', str(value)):
            raise ValueError()
        result = int(value)
        if not minimum <= result <= maximum:
            raise ValueError()
        return result
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f'{setting} must be an integer between {minimum} and {maximum}') from None

def parse_legacy_probe_timeout(value):
    return bounded_legacy_probe_integer(value, 6, 60, 'Legacy probe timeout')

def parse_legacy_probe_attempts(value):
    return bounded_legacy_probe_integer(value, 1, 3, 'Legacy probe attempts')

def legacy_confirmation_evidence(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value):
        raise argparse.ArgumentTypeError('Legacy confirmation evidence must be the displayed 64-character SHA-256 fingerprint')
    return value

def sanitized_confirmation_summary(summary, *, evidence_only=False):
    """Accept only the companion's fixed, non-secret review-evidence schema."""
    sources = {'TZ', 'JAVA_TOOL_OPTIONS', 'JDK_JAVA_OPTIONS', '_JAVA_OPTIONS', 'cmdline',
               'etc-timezone', 'etc-localtime'}
    statuses = {'matches', 'mismatch', 'ambiguous', 'unknown'}
    def sha256(value):
        return isinstance(value, str) and re.fullmatch(r'[a-f0-9]{64}', value) is not None
    def zone(value):
        if not isinstance(value, str): return False
        if value in BACKEND_TIME_ZONES: return True
        match = re.fullmatch(r'GMT[+-]([0-9]{1,2})(?::([0-9]{2}))?', value)
        return bool(match and int(match[1]) <= 23 and int(match[2] or '0') <= 59)
    def bounded_list(value, valid):
        return isinstance(value, list) and len(value) <= 160 and all(valid(item) for item in value)
    def source_values(value, valid):
        return isinstance(value, dict) and set(value) <= sources and \
            all(bounded_list(items, valid) for items in value.values())
    try:
        actual = evidence_only and isinstance(summary, dict) and 'observedZone' in summary
        required = {'schemaVersion', 'expectedZone', 'containerId', 'imageIdentitySha256',
                    'containerStartIdentitySha256', 'configured'} | \
                   ({'observedZone', 'pid'} if actual else {'probeUnavailableReason'})
        optional = {'pidStartTicks'} if actual else {'pid', 'pidStartTicks', 'startupInputs'}
        if not isinstance(summary, dict) or not required <= set(summary) or \
                set(summary) - required - optional or \
                type(summary['schemaVersion']) is not int or summary['schemaVersion'] != 1 or \
                summary['expectedZone'] not in BACKEND_TIME_ZONES or \
                not all(sha256(summary[key]) for key in ('containerId', 'imageIdentitySha256', 'containerStartIdentitySha256')):
            raise ValueError()
        if actual:
            if not zone(summary['observedZone']): raise ValueError()
        elif summary['probeUnavailableReason'] not in {'missing_tools', 'ambiguous_jvm', 'attach_failed',
                                                       'missing_or_unknown_property'}:
            raise ValueError()
        configured = summary['configured']
        if not isinstance(configured, dict) or set(configured) != {'status', 'sources', 'zones'} or \
                configured['status'] not in statuses or \
                not bounded_list(configured['sources'], lambda item: item in sources - {'cmdline', 'etc-timezone', 'etc-localtime'} | {'JAVA_OPTS'}) or \
                not bounded_list(configured['zones'], zone):
            raise ValueError()
        if 'pid' in summary and (type(summary['pid']) is not int or summary['pid'] <= 0):
            raise ValueError()
        if 'pidStartTicks' in summary and (not isinstance(summary['pidStartTicks'], str) or
                not re.fullmatch(r'[1-9][0-9]*', summary['pidStartTicks'])):
            raise ValueError()
        if 'startupInputs' in summary:
            startup = summary['startupInputs']
            keys = {'status', 'basis', 'zones', 'processInputsComplete', 'reads', 'localtimeSha256',
                    'requestedTzifDigestMatches', 'sourceKinds', 'observedZones', 'localtimeResolution',
                    'platformInputsConflict'}
            startup_statuses = statuses | ({'unresolved'} if evidence_only else set())
            if not isinstance(startup, dict) or set(startup) != keys or startup['status'] not in startup_statuses or \
                    startup['basis'] not in {'jvm-startup-properties', 'process-TZ', 'platform-files-jdk-version-unspecified'} or \
                    not bounded_list(startup['zones'], zone) or type(startup['processInputsComplete']) is not bool or \
                    type(startup['platformInputsConflict']) is not bool or \
                    type(startup['requestedTzifDigestMatches']) is not bool or \
                    startup['reads'].keys() != {'environment', 'cmdline'} or \
                    any(value not in {'ok', 'unavailable'} for value in startup['reads'].values()) or \
                    not (startup['localtimeSha256'] == 'unavailable' or sha256(startup['localtimeSha256'])) or \
                    not source_values(startup['sourceKinds'], lambda item: item in {'zone', 'unsupported', 'missing'}) or \
                    not source_values(startup['observedZones'], zone) or \
                    startup['localtimeResolution'] not in {'symlink', 'resolved', 'missing', 'unresolved'}:
                raise ValueError()
        return json.loads(json.dumps(summary, sort_keys=True, separators=(',', ':'), ensure_ascii=True))
    except (AttributeError, KeyError, TypeError, ValueError):
        raise RuntimeError('Legacy confirmation evidence summary is invalid') from None

def sanitized_legacy_evidence_report(report):
    """Validate inspection diagnostics without turning a blocked SHA into approval."""
    reasons = {
        'running-jvm': {'observed_jvm_match'},
        'confirmation-required': {'operator_confirmation_required'},
        'blocked': {'observed_jvm_mismatch', 'localtime_unresolved', 'platform_conflict',
                    'startup_mismatch', 'configuration_mismatch'},
    }
    try:
        if not isinstance(report, dict) or set(report) != {'status', 'reason', 'evidence', 'evidenceSha256'} or \
                report['status'] not in reasons or report['reason'] not in reasons[report['status']]:
            raise ValueError()
        evidence = sanitized_confirmation_summary(report['evidence'], evidence_only=True)
        actual = 'observedZone' in evidence
        if actual != (report['reason'] in {'observed_jvm_match', 'observed_jvm_mismatch'}):
            raise ValueError()
        if report['status'] == 'confirmation-required' and \
                evidence.get('startupInputs', {}).get('status') in {'mismatch', 'unresolved'}:
            raise ValueError()
        if report['status'] == 'confirmation-required' and 'startupInputs' not in evidence and \
                evidence['configured']['status'] == 'mismatch':
            raise ValueError()
        fingerprint = legacy_confirmation_evidence(report['evidenceSha256'])
        canonical = json.dumps(evidence, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
        if hashlib.sha256(canonical.encode()).hexdigest() != fingerprint:
            raise ValueError()
        return {'status': report['status'], 'reason': report['reason'], 'evidence': evidence,
                'evidenceSha256': fingerprint}
    except (AttributeError, KeyError, TypeError, ValueError, RuntimeError, argparse.ArgumentTypeError):
        raise RuntimeError('Legacy inspection evidence report is invalid') from None

class LegacyTimeZoneConfirmationRequired(RuntimeError):
    """Only the digest-bound companion can supply the sanitized evidence summary."""
    def __init__(self, summary, fingerprint):
        self.summary = sanitized_confirmation_summary(summary)
        self.fingerprint = legacy_confirmation_evidence(fingerprint)
        canonical = json.dumps(self.summary, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
        if hashlib.sha256(canonical.encode()).hexdigest() != self.fingerprint:
            raise RuntimeError('Legacy confirmation evidence fingerprint is invalid')
        super().__init__('Review the displayed Legacy time-zone evidence and repeat prepare with both confirmation flags')

def require_job_budget(deadline, reserve=DEPLOYMENT_RESERVE_SECONDS):
    if deadline is not None and deadline - time.time() < reserve:
        raise RuntimeError('Insufficient job time remains; deployment was not started. Inspect status and retry in a fresh job')

@contextlib.contextmanager
def deployment_lock(timeout, command, job_deadline_epoch=None, *, read_only=False):
    require_job_budget(job_deadline_epoch)
    if job_deadline_epoch is not None:
        timeout = min(timeout, max(0, job_deadline_epoch - time.time() - DEPLOYMENT_RESERVE_SECONDS))
    try:
        lock_file = (HOME / 'deploy.lock').open('r' if read_only else 'a')
    except OSError:
        if read_only:
            raise RuntimeError('Legacy evidence inspection requires an accessible existing deployment lock') from None
        raise
    with lock_file as lock:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError('Deployment lock is busy; operation was not started. Inspect status and coordinate with its owner')
                time.sleep(min(0.25, max(0, deadline - time.monotonic())))
        # Recheck after acquisition: waiting and wall-clock changes must not consume the operation reserve.
        require_job_budget(job_deadline_epoch)
        if read_only:
            # The existing file and owner advisory stay byte-identical. The OS
            # exclusive flock still serializes inspection with deployment.
            yield
            return
        owner_path = HOME / 'lock-owner.json'
        owner = {'pid': os.getpid(), 'command': command,
                 'startedAt': datetime.now(timezone.utc).isoformat(), 'lease': secrets.token_hex(8)}
        try:
            atomic(owner_path, json.dumps(owner))
            yield
        finally:
            # The record is advisory after a killed process; the OS file lock is authoritative.
            with contextlib.suppress(OSError, ValueError):
                if json.loads(owner_path.read_text()).get('lease') == owner['lease']:
                    owner_path.unlink()

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

def backend_identity(color):
    """Pin the actual managed process before any transition can change its admission state."""
    if color not in IPS:
        return None
    info = inspect(name(color, 'backend'))
    labels = info['Config'].get('Labels', {}) or {}
    expected_image = state().get('slots', {}).get(color, {}).get('images', {}).get('backend')
    if labels.get('ieum.bluegreen.managed') != 'true' or \
            labels.get('ieum.bluegreen.color') != color or info['Image'] != expected_image:
        raise RuntimeError('Backend identity does not match its managed release')
    if not re.fullmatch(r'[a-f0-9]{64}', info.get('Id', '')):
        raise RuntimeError('Cannot pin the backend container ID')
    return {'id': info['Id'], 'image': info['Image'], 'name': name(color, 'backend'),
            'restartPolicy': info['HostConfig']['RestartPolicy']}

def same_backend(actual, expected):
    return expected is not None and all(actual.get(key) == expected.get(key)
                                        for key in ('id', 'image', 'name'))

def verify_backend_identity(transaction, color):
    if color in IPS and not same_backend(backend_identity(color), transaction.get('backends', {}).get(color)):
        raise RuntimeError('Backend identity changed or was not pinned; manual inspection required')

def restore_restart_policy(transaction, color):
    expected = transaction.get('backends', {}).get(color)
    if not same_backend(backend_identity(color), expected):
        raise RuntimeError('Isolated backend identity changed; manual inspection required')
    policy = expected.get('restartPolicy', {})
    policy_name = policy.get('Name')
    retries = policy.get('MaximumRetryCount', 0)
    if policy_name not in ('no', 'always', 'unless-stopped', 'on-failure') or \
            type(retries) is not int or retries < 0:
        raise RuntimeError('Original backend restart policy is unavailable; inspect journal manually')
    value = policy_name + (':' + str(retries) if policy_name == 'on-failure' and retries else '')
    docker('update', '--restart=' + value, expected['id'])
    actual = backend_identity(color)
    if not same_backend(actual, expected) or actual['restartPolicy'].get('Name') != policy_name or \
            actual['restartPolicy'].get('MaximumRetryCount', 0) != retries:
        raise RuntimeError('Restored backend restart policy could not be verified')

def transaction_images(transaction, color):
    # An authoritatively absent, isolated backend cannot be inspected again. Its
    # immutable release inventory was pinned before the transition started.
    if color in transaction.get('isolated', {}):
        images = transaction.get('snapshot', {}).get('slots', {}).get(color, {}).get('images', {})
        if set(images) != set(APPS):
            raise RuntimeError('Isolated release inventory is unavailable; inspect journal manually')
        return images
    return slot_images(color)

def write_transaction(transaction):
    atomic(HOME / 'transaction.json', json.dumps(transaction))

def quarantine_backend(transaction, color):
    """A timed-out control request is not evidence that a worker stopped. Fence by Docker ID."""
    expected = transaction.get('backends', {}).get(color)
    if color not in IPS or not expected or not re.fullmatch(r'[a-f0-9]{64}', expected.get('id', '')):
        raise RuntimeError('Recovery requires a pinned managed backend identity; inspect old journal manually')
    # `exists()` intentionally returns False on any inspect failure. It must not be used
    # as proof of isolation: an unavailable Docker daemon is not an absent container.
    inventory = [line.split(maxsplit=1) for line in
        docker('ps', '--all', '--no-trunc', '--format', '{{.ID}} {{.Names}}').splitlines() if line.strip()]
    names = {container_name: container_id for container_id, container_name in inventory}
    if expected['name'] not in names:
        if expected['id'] in names.values():
            raise RuntimeError('Pinned backend was renamed; refusing to isolate an unexpected container')
        transaction.setdefault('isolated', {})[color] = expected
        write_transaction(transaction)
        return
    def pinned():
        info = inspect(expected['name'])
        labels = info['Config'].get('Labels', {}) or {}
        if info.get('Id') != expected['id'] or info['Image'] != expected['image'] or \
                labels.get('ieum.bluegreen.managed') != 'true' or \
                labels.get('ieum.bluegreen.color') != color:
            raise RuntimeError('Backend container changed; refusing to stop a replacement')
        return info
    info = pinned()
    transaction['stage'] = 'isolating_' + color
    write_transaction(transaction)
    docker('update', '--restart=no', expected['id'])
    if info['State']['Status'] in ('running', 'restarting', 'paused'):
        docker('stop', '--timeout', str(ISOLATION_TIMEOUT), expected['id'])
    info = pinned()
    if info['State'].get('Running') or info['State']['Status'] in ('running', 'restarting', 'paused') or \
            info['HostConfig']['RestartPolicy']['Name'] != 'no':
        raise RuntimeError('Backend isolation could not be verified; activation refused')
    transaction.setdefault('isolated', {})[color] = expected
    write_transaction(transaction)
    record('backend_isolated', environment=color, containerId=expected['id'])

def drain_or_isolate(transaction, color, force=False):
    try:
        control(color, 'drain')
    except Exception as failure:
        if not force:
            raise RuntimeError('Backend drain failed; use explicit --force to isolate it before switching') from failure
        quarantine_backend(transaction, color)

def normalized_cutoff(value):
    try:
        instant = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError('missing UTC offset')
        return instant.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    except (ValueError, AttributeError) as failure:
        raise RuntimeError('Pending recovery cutoff requires an ISO-8601 timestamp with Z or UTC offset') from failure

def pending_recovery_cutoff(explicit=None, persist=True):
    path = HOME / 'control/pending-recovery-not-before'
    saved = normalized_cutoff(path.read_text().strip()) if path.exists() else None
    supplied = normalized_cutoff(explicit) if explicit else None
    if saved and supplied and saved != supplied:
        raise RuntimeError('Pending recovery cutoff is immutable across deployments; review historical work separately')
    value = saved or supplied
    if value is None:
        raise RuntimeError('Review existing PENDING rows and supply --pending-recovery-not-before before protocol-2 deployment')
    if saved is None and persist:
        atomic(path, value + '\n', 0o644)
        record('pending_recovery_cutoff_initialized', notBefore=value)
    return value

def normalized_backend_zone(value):
    try:
        if not isinstance(value, str) or value != value.strip() or not value or len(value) > 128 or \
                value not in BACKEND_TIME_ZONES:
            raise ValueError()
        ZoneInfo(value)
        return value
    except (ZoneInfoNotFoundError, ValueError, TypeError) as failure:
        raise RuntimeError('Backend time zone requires a reviewed IANA zone identifier, for example UTC or Asia/Seoul') from failure

def backend_time_zone(explicit=None, persist=True):
    path = HOME / 'control/backend-time-zone'
    saved = normalized_backend_zone(path.read_text().strip()) if path.exists() else None
    supplied = normalized_backend_zone(explicit) if explicit else None
    if saved and supplied and saved != supplied:
        raise RuntimeError('Backend time zone is immutable across deployments; review existing timestamp semantics separately')
    value = saved or supplied
    if value is None:
        raise RuntimeError('Confirm the Legacy JVM timestamp zone and supply --backend-time-zone or the expected-zone backend setting')
    if saved is None and persist:
        atomic(path, value + '\n', 0o644)
        record('backend_time_zone_initialized', zone=value)
    return value

def legacy_time_zone_companion():
    # The approved controller digest also binds its companion. Execute the exact verified bytes.
    path = Path(__file__).with_name('legacy-time-zone.py')
    try:
        source = path.read_bytes()
        if hashlib.sha256(source).hexdigest() != LEGACY_ZONE_HELPER_SHA256:
            raise ValueError()
    except (OSError, ValueError):
        raise RuntimeError('Legacy time-zone companion is missing or differs from the approved controller bundle') from None
    namespace = {'__name__': 'ieum_legacy_time_zone', '__file__': str(path)}
    exec(compile(source, str(path), 'exec'), namespace)
    def probe(*args, timeout):
        try:
            result = subprocess.run(['docker', *args], text=True, capture_output=True, timeout=timeout)
            if result.returncode:
                raise RuntimeError()
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            raise TimeoutError('Legacy JVM evidence probe timed out') from None
        except OSError as failure:
            # Docker daemon connection errors normally return a nonzero CLI exit
            # and fail immediately above. This branch only classifies launch errors.
            if failure.errno in (errno.ENOTCONN, errno.ECONNREFUSED, errno.ECONNRESET,
                                 errno.ETIMEDOUT, errno.EHOSTUNREACH, errno.ENETUNREACH):
                raise ConnectionError('Legacy JVM evidence probe transport failed') from None
            raise RuntimeError('Legacy JVM evidence probe command failed') from None
        except (subprocess.SubprocessError, RuntimeError):
            raise RuntimeError('Legacy JVM evidence probe failed') from None
    return namespace, probe

def validate_legacy_time_zone(snapshot, requested, confirmation_id,
                              confirmation_evidence=None,
                              probe_timeout=DEFAULT_LEGACY_PROBE_TIMEOUT,
                              probe_attempts=DEFAULT_LEGACY_PROBE_ATTEMPTS):
    probe_timeout = parse_legacy_probe_timeout(probe_timeout)
    probe_attempts = parse_legacy_probe_attempts(probe_attempts)
    namespace, probe = legacy_time_zone_companion()
    try:
        return namespace['validate_legacy_zone'](snapshot, requested, confirmation_id, probe,
            confirmation_evidence=confirmation_evidence, probe_timeout=probe_timeout, probe_attempts=probe_attempts)
    except namespace['LegacyConfirmationRequired'] as failure:
        raise LegacyTimeZoneConfirmationRequired(failure.summary, failure.fingerprint) from None

def inspect_legacy_time_zone(snapshot, requested,
                             probe_timeout=DEFAULT_LEGACY_PROBE_TIMEOUT,
                             probe_attempts=DEFAULT_LEGACY_PROBE_ATTEMPTS):
    probe_timeout = parse_legacy_probe_timeout(probe_timeout)
    probe_attempts = parse_legacy_probe_attempts(probe_attempts)
    namespace, probe = legacy_time_zone_companion()
    report = namespace['inspect_legacy_zone'](snapshot, requested, probe,
        probe_timeout=probe_timeout, probe_attempts=probe_attempts)
    return sanitized_legacy_evidence_report(report)

def legacy_zone_evidence(backend_zone=None,
                         legacy_probe_timeout=DEFAULT_LEGACY_PROBE_TIMEOUT,
                         legacy_probe_attempts=DEFAULT_LEGACY_PROBE_ATTEMPTS):
    """Inspect Legacy time-zone evidence without any deployment-file mutation."""
    legacy_probe_timeout = parse_legacy_probe_timeout(legacy_probe_timeout)
    legacy_probe_attempts = parse_legacy_probe_attempts(legacy_probe_attempts)
    try:
        data = state()
        if data['active'] != 'legacy':
            raise RuntimeError()
        assert_consistent(data)
        if (HOME / 'transaction.json').exists():
            raise RuntimeError()
        expected_zone = backend_time_zone(backend_zone, persist=False)
        snapshots = capture_legacy_snapshot()
        report = inspect_legacy_time_zone(snapshots['backend'], expected_zone,
            probe_timeout=legacy_probe_timeout, probe_attempts=legacy_probe_attempts)
        report = sanitized_legacy_evidence_report(report)
        expected_identity = {
            'expectedZone': expected_zone, 'containerId': snapshots['backend']['containerId'],
            'imageIdentitySha256': hashlib.sha256(snapshots['backend']['image'].encode()).hexdigest(),
            'containerStartIdentitySha256': hashlib.sha256(snapshots['backend']['startedAt'].encode()).hexdigest(),
        }
        if any(report['evidence'][key] != value for key, value in expected_identity.items()):
            raise RuntimeError()
        verify_legacy_snapshot(snapshots)
        # Revalidate even mocked/adapted producers at the last output boundary.
        return sanitized_legacy_evidence_report(report)
    except Exception as failure:
        code = getattr(failure, 'code', None)
        if not isinstance(code, str) or code not in LEGACY_PROBE_FAILURE_CODES:
            code = 'legacy_evidence_unverified'
        raise RuntimeError('Legacy time-zone evidence unavailable [' + code +
            ']; inspect initialized Legacy state, identity and bounded probe inputs') from None

def run_legacy_zone_evidence(args):
    """The inspection CLI has a fixed diagnostic boundary and never initializes files."""
    try:
        if not HOME.is_dir() or not (HOME / 'state.json').is_file() or not (HOME / 'deploy.lock').is_file():
            raise RuntimeError()
        wait_seconds = default_lock_timeout(args.command) if args.lock_timeout is None else args.lock_timeout
        with deployment_lock(wait_seconds, args.command, read_only=True):
            report = legacy_zone_evidence(args.backend_time_zone,
                legacy_probe_timeout=args.legacy_probe_timeout, legacy_probe_attempts=args.legacy_probe_attempts)
            report = sanitized_legacy_evidence_report(report)
            print(json.dumps(report, sort_keys=True, ensure_ascii=True))
            return 1 if report['status'] == 'blocked' else 0
    except Exception:
        print('Legacy time-zone evidence unavailable; inspect initialized Legacy state, identity and bounded probe inputs',
              file=sys.stderr)
        return 1

def record_legacy_probe_failure(failure, timeout, attempt_limit):
    if isinstance(failure, LegacyTimeZoneConfirmationRequired):
        diagnostic = {'event': 'legacy_zone_confirmation_required', 'evidence': failure.summary,
                      'evidenceSha256': failure.fingerprint}
        print(json.dumps(diagnostic, sort_keys=True, ensure_ascii=True), file=sys.stderr)
        record('legacy_zone_confirmation_required', evidence=failure.summary,
               evidenceSha256=failure.fingerprint)
        return
    code, attempts = getattr(failure, 'code', None), getattr(failure, 'attempts', None)
    details = {'configuredTimeout': timeout, 'configuredAttempts': attempt_limit, 'attemptsKnown': False,
               'code': 'legacy_zone_validation_failed'}
    if isinstance(code, str) and code in LEGACY_PROBE_FAILURE_CODES and type(attempts) is int and 1 <= attempts <= attempt_limit:
        details.update(code=code, attempts=attempts, attemptsKnown=True)
    record('legacy_zone_probe_failed', **details)

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

def remove_inactive(color, force=False):
    data = state()
    assert_consistent(data)
    if data['active'] == color:
        raise RuntimeError('Refusing to replace the active environment')
    # Validate all ownership labels before touching any application or release metadata.
    for app in APPS:
        container = name(color, app)
        if exists(container) and inspect(container)['Config'].get('Labels', {}).get('ieum.bluegreen.managed') != 'true':
            raise RuntimeError('Refusing to remove unmanaged container: ' + container)
    replacement_transaction = None
    try:
        drain(color)
    except Exception:
        if not force:
            raise
        replacement_transaction = {'from': data['active'], 'to': color, 'started': time.time(),
            'stage': 'replacing_after_failed_drain', 'snapshot': json.loads(json.dumps(data)),
            'targetImages': data.get('slots', {}).get(color, {}).get('images'),
            'backends': {c: backend_identity(c) for c in (data['active'], color) if c in IPS}}
        write_transaction(replacement_transaction)
        quarantine_backend(replacement_transaction, color)
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
    if replacement_transaction is not None:
        (HOME / 'transaction.json').unlink()

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

def env_values(app, snapshot, active, override=None, replace=False):
    if replace:
        if override is None:
            raise RuntimeError('Replacing environment requires an explicit private env file')
        return read_env(override)
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

def capture_legacy_snapshot():
    """Read current deployed configuration; init's snapshot is historical rollback evidence only."""
    snapshots = {}
    for app in APPS:
        info = inspect(LEGACY[app])
        if info['State']['Status'] != 'running' or not re.fullmatch(r'[a-f0-9]{64}', info.get('Id', '')):
            raise RuntimeError('Legacy source must be running with a pinned container ID before preparation')
        runtime = dict(e.split('=', 1) for e in info['Config'].get('Env', []))
        image = json.loads(docker('image', 'inspect', info['Image']))[0]
        defaults = dict(e.split('=', 1) for e in image['Config'].get('Env', []))
        snapshots[app] = {'image': info['Image'], 'containerId': info['Id'], 'runtimeEnvironment': runtime,
            'startedAt': info['State']['StartedAt'],
            'environment': {key: value for key, value in runtime.items() if defaults.get(key) != value}}
    return snapshots

def verify_legacy_snapshot(snapshots):
    for app in APPS:
        info = inspect(LEGACY[app])
        runtime = dict(e.split('=', 1) for e in info['Config'].get('Env', []))
        if info.get('Id') != snapshots[app]['containerId'] or info['Image'] != snapshots[app]['image'] or \
                info['State']['Status'] != 'running' or info['State']['StartedAt'] != snapshots[app]['startedAt'] or \
                runtime != snapshots[app]['runtimeEnvironment']:
            raise RuntimeError('Legacy source changed during preparation; freeze old workflows and retry')

def cleanup_candidate_containers(color, created, unidentified=None):
    """Clean only this preparation's pinned IDs, within one bounded failure budget."""
    deadline = time.monotonic() + CANDIDATE_CLEANUP_TIMEOUT
    results = []

    def unverified(reason):
        return {'complete': False, 'containers': [{**item, 'removed': False, 'reason': reason}
                for item in created] + ([{**unidentified, 'removed': False, 'reason': 'identity_unavailable'}]
                                       if unidentified else [])}

    if created:
        try:
            if state()['active'] == color or route_color() == color or \
                    (HOME / 'control/active').read_text().strip() == color:
                return unverified('active_environment')
        except (OSError, KeyError, ValueError, RuntimeError):
            return unverified('active_state_unverified')

    def command(*args, timeout=CANDIDATE_DOCKER_TIMEOUT):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError('budget_exhausted')
        try:
            result = subprocess.run(['docker', *args], text=True, capture_output=True,
                                    timeout=min(timeout, remaining))
        except subprocess.TimeoutExpired:
            raise RuntimeError('command_timeout') from None
        except (OSError, subprocess.SubprocessError):
            raise RuntimeError('docker_unavailable') from None
        if result.returncode:
            raise RuntimeError('docker_operation_failed')
        return result.stdout.rstrip('\n')

    def container_ids(output):
        identities = output.splitlines()
        if any(not re.fullmatch(r'[a-f0-9]{64}', identity) for identity in identities) or \
                len(set(identities)) != len(identities):
            raise RuntimeError('inventory_unverified')
        return set(identities)

    try:
        # Pinned IDs do not depend on unrelated legacy link aliases in .Names.
        inventory = container_ids(command('ps', '--all', '--no-trunc', '--format', '{{.ID}}')) if created else set()
    except RuntimeError as failure:
        return unverified(str(failure))
    for item in reversed(created):
        outcome = {**item, 'removed': False}
        try:
            if item['id'] not in inventory:
                outcome['removed'] = True
                outcome['reason'] = 'already_absent'
                continue
            info = json.loads(command('inspect', item['id']))[0]
            labels = info['Config'].get('Labels', {}) or {}
            if info.get('Id') != item['id'] or info.get('Image') != item['image'] or \
                    info.get('Name') != '/' + item['name'] or \
                    labels.get('ieum.bluegreen.managed') != 'true' or \
                    labels.get('ieum.bluegreen.color') != color:
                raise RuntimeError('identity_changed')
            # A failed stop must not leave a fresh candidate restarting unattended.
            command('update', '--restart=no', item['id'])
            if info['State']['Status'] in ('running', 'restarting', 'paused'):
                command('stop', '--time', str(CANDIDATE_STOP_TIMEOUT), item['id'],
                        timeout=CANDIDATE_STOP_TIMEOUT + 5)
            command('rm', item['id'])
            outcome['removed'] = True
        except (KeyError, IndexError, TypeError, ValueError):
            outcome['reason'] = 'identity_unverified'
        except RuntimeError as failure:
            outcome['reason'] = str(failure)
        finally:
            results.append(outcome)
    if unidentified is not None:
        # Only an authoritative exact-name absence proves that an unacknowledged
        # create left no container. Never adopt or mutate a same-name container.
        outcome = {**unidentified, 'removed': False}
        try:
            if color not in IPS:
                raise RuntimeError('identity_unavailable')
            expected_name = name(color, next(app for app in APPS if name(color, app) == unidentified['name']))
            matching = container_ids(command('ps', '--all', '--no-trunc', '--filter',
                'name=^/' + expected_name + '$', '--format', '{{.ID}}'))
            if len(matching) > 1:
                raise RuntimeError('inventory_unverified')
            outcome.update(removed=not matching, reason='identity_unavailable' if matching else 'already_absent')
        except (KeyError, StopIteration):
            outcome['reason'] = 'identity_unavailable'
        except RuntimeError as failure:
            outcome['reason'] = str(failure)
        results.append(outcome)
    return {'complete': all(item['removed'] for item in results), 'containers': results}

def prepare(color, overrides, env_overrides=None, replace_env=False, recovery_not_before=None, force=False,
            backend_zone=None, legacy_zone_confirmation=None, legacy_zone_evidence=None,
            legacy_probe_timeout=DEFAULT_LEGACY_PROBE_TIMEOUT,
            legacy_probe_attempts=DEFAULT_LEGACY_PROBE_ATTEMPTS):
    legacy_probe_timeout = parse_legacy_probe_timeout(legacy_probe_timeout)
    legacy_probe_attempts = parse_legacy_probe_attempts(legacy_probe_attempts)
    if legacy_zone_evidence is not None:
        legacy_zone_evidence = legacy_confirmation_evidence(legacy_zone_evidence)
    data = state()
    assert_consistent(data)
    if color == data['active']: raise RuntimeError('Target environment is already active')
    if (legacy_zone_confirmation is not None or legacy_zone_evidence is not None) and data['active'] != 'legacy':
        raise RuntimeError('Legacy time-zone confirmation is only valid during Legacy preparation')
    env_overrides = {} if env_overrides is None else env_overrides
    if set(env_overrides) - set(overrides):
        raise RuntimeError('Environment changes require an explicit image deployment of the same service')
    if replace_env and not env_overrides:
        raise RuntimeError('--replace-env requires --env-file')
    snapshots = capture_legacy_snapshot() if data['active'] == 'legacy' else {
        app: {'image': inspect(name(data['active'], app))['Image']} for app in APPS}
    images = {app: json.loads(docker('image', 'inspect', overrides.get(app, snapshots[app]['image'])))[0]
              for app in APPS}
    if images['backend']['Config'].get('Labels', {}).get('ieum.bluegreen.capable') != 'true':
        raise RuntimeError('Backend image lacks distributed deployment support')
    if images['backend']['Config'].get('Labels', {}).get('ieum.bluegreen.protocol') != str(PROTOCOL_VERSION):
        raise RuntimeError('Deploy the corrected protocol-2 backend before frontend/agent deployments')
    environments = {app: env_values(app, snapshots[app], data['active'], env_overrides.get(app),
        replace=replace_env and app in env_overrides) for app in APPS}
    if any('\n' in value or '\r' in value for env in environments.values() for value in env.values()):
        raise RuntimeError('Multiline environment values require an explicit deployment configuration')
    configured_cutoff = environments['backend'].get('IEUM_WORKFLOW_QUEUE_PENDING_RECOVERY_NOT_BEFORE')
    if recovery_not_before and configured_cutoff and normalized_cutoff(recovery_not_before) != normalized_cutoff(configured_cutoff):
        raise RuntimeError('CLI and backend environment specify different pending recovery cutoffs')
    cutoff = pending_recovery_cutoff(recovery_not_before or configured_cutoff, persist=False)
    configured_zone = environments['backend'].get('IEUM_WORKFLOW_QUEUE_PENDING_RECOVERY_EXPECTED_ZONE')
    if backend_zone and configured_zone and normalized_backend_zone(backend_zone) != normalized_backend_zone(configured_zone):
        raise RuntimeError('CLI and backend environment specify different expected time zones')
    expected_zone = backend_time_zone(backend_zone or configured_zone, persist=False)
    if data['active'] == 'legacy':
        try:
            proof = validate_legacy_time_zone(snapshots['backend'], expected_zone, legacy_zone_confirmation,
                confirmation_evidence=legacy_zone_evidence,
                probe_timeout=legacy_probe_timeout, probe_attempts=legacy_probe_attempts)
            verify_legacy_snapshot(snapshots)
        except Exception as failure:
            record_legacy_probe_failure(failure, legacy_probe_timeout, legacy_probe_attempts)
            raise
        atomic(HOME / 'control/backend-time-zone-proof.json', json.dumps({
            'expectedZone': expected_zone, 'verifiedAt': datetime.now(timezone.utc).isoformat(), **proof}))
        record('legacy_backend_time_zone_verified', expectedZone=expected_zone, **proof)
        record('legacy_source_captured', images={a: snapshots[a]['image'] for a in APPS},
               containerIds={a: snapshots[a]['containerId'] for a in APPS})
    pending_recovery_cutoff(cutoff)
    backend_time_zone(expected_zone)
    if force: remove_inactive(color, force=True)
    else: remove_inactive(color)
    data = state()  # remove_inactive invalidates the former rollback release.
    image_ids = {app: images[app]['Id'] for app in APPS}
    data['slots'][color] = {'status': 'preparing', 'images': image_ids, 'release': release_id(image_ids)}
    save(data)
    created = []
    pending_creation = None
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
                    'IEUM_WORKFLOW_QUEUE_PENDING_RECOVERY_NOT_BEFORE': cutoff,
                    'TZ': expected_zone, 'IEUM_WORKFLOW_QUEUE_PENDING_RECOVERY_EXPECTED_ZONE': expected_zone,
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
            pending_creation = {'name': name(color, app), 'image': images[app]['Id']}
            container_id = docker(*args, images[app]['Id'])
            if not re.fullmatch(r'[a-f0-9]{64}', container_id):
                raise RuntimeError('Created candidate container ID could not be pinned')
            created.append({**pending_creation, 'id': container_id})
            pending_creation = None
            docker('start', container_id)
        details = check(color)
        if not details.get('draining') or not details.get('schedulerStandby'):
            raise RuntimeError('Candidate background workers started before activation')
        if data['active'] == 'legacy':
            verify_legacy_snapshot(snapshots)
        data['slots'][color]['status'] = 'candidate'
        save(data)
        record('prepared', target=color, images=data['slots'][color])
        print('Prepared and healthy:', color)
    except Exception as original_error:
        cleanup = cleanup_candidate_containers(color, created, pending_creation)
        invalidate_slot(color, 'failed')
        failed = state()
        failed['slots'][color]['cleanup'] = cleanup
        save(failed)
        record('prepare_failed', target=color, cleanup=cleanup)
        if not cleanup['complete']:
            raise RuntimeError('Candidate preparation failed; cleanup is incomplete. Inspect status before retrying') from original_error
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


def switch(color, force=False):
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
                   'snapshot': original_data, 'targetImages': target_slot['images'],
                   'backends': {c: backend_identity(c) for c in (old, color) if c in IPS}}
    write_transaction(transaction)
    try:
        if old in IPS:
            drain_or_isolate(transaction, old, force)
        control(color, 'resume')
        route(color)
        # Probe the same TLS virtual host, independently of external pfSense NAT.
        verify_ingress(color)
        data['active'], data['previous'] = color, old
        if old in IPS:
            old_images = transaction_images(transaction, old)
            isolated = old in transaction.get('isolated', {})
            data['slots'][old] = {'status': 'isolated' if isolated else 'retained',
                'images': old_images, 'release': release_id(old_images)}
            if isolated: data['previous'] = None
        else:
            data['previous'] = None
        data['slots'][color]['status'] = 'active'
        atomic(HOME / 'control/active', color + '\n', 0o644)
        save(data)
        transaction['stage'] = 'committed'
        write_transaction(transaction)
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

def restore_previous(transaction, allow_isolated=False):
    old, candidate = transaction['from'], transaction['to']
    original = json.loads(json.dumps(transaction.get('snapshot')))
    if original is None:
        raise RuntimeError('Old-format transaction requires manual inspection before recovery')
    if old in IPS and slot_images(old) != original.get('slots', {}).get(old, {}).get('images'):
        raise RuntimeError('Original release images changed; refusing to resume an unknown release')
    verify_backend_identity(transaction, old)
    was_isolated = old in transaction.get('isolated', {})
    if was_isolated:
        if not allow_isolated:
            raise RuntimeError('Original backend was isolated; recover a healthy target or explicitly abandon for forward-fix')
        check(old)  # Only an explicitly chosen, already restarted healthy release may be resumed.
        if not same_backend(backend_identity(old), transaction['backends'].get(old)):
            raise RuntimeError('Isolated backend identity changed; manual inspection required')
    route(old)
    atomic(HOME / 'control/active', old + '\n', 0o644)
    # Restore useful work first. An unreachable candidate must never leave the healthy
    # environment's queue/Quartz stopped while HTTP appears recovered.
    if old in IPS:
        control(old, 'resume')
    try:
        verify_backend_identity(transaction, candidate)
        control(candidate, 'drain')
    except Exception:
        quarantine_backend(transaction, candidate)
    previous_slot = original.get('slots', {}).get(candidate, {})
    can_retry = (previous_slot.get('status') == 'retained'
        and candidate not in transaction.get('isolated', {})
        and slot_images(candidate) == previous_slot.get('images'))
    if not can_retry:
        if original.get('previous') == candidate:
            original['previous'] = None
        previous_slot['status'] = 'isolated' if candidate in transaction.get('isolated', {}) else 'failed'
    save(original)
    verify_ingress(old)
    if was_isolated:
        restore_restart_policy(transaction, old)
        del transaction['isolated'][old]
        write_transaction(transaction)
    record('switch_failed_rolled_back', active=old, candidate=candidate)
    (HOME / 'transaction.json').unlink(missing_ok=True)

def finish_target(transaction, force=False):
    target, old = transaction['to'], transaction['from']
    if slot_images(target) != transaction.get('targetImages'):
        raise RuntimeError('Target release images changed; manual inspection required')
    verify_backend_identity(transaction, target)
    check(target)
    was_isolated = target in transaction.get('isolated', {})
    if was_isolated:
        if not force or not same_backend(backend_identity(target), transaction['backends'].get(target)):
            raise RuntimeError('Resuming an isolated target requires --force and its original container identity')
    if old in IPS:
        drain_or_isolate(transaction, old, force)
    control(target, 'resume')
    if route_color() != target:
        route(target)
    verify_ingress(target)
    if was_isolated:
        restore_restart_policy(transaction, target)
        del transaction['isolated'][target]
        write_transaction(transaction)
    data = json.loads(json.dumps(transaction.get('snapshot')))
    if data is None:
        raise RuntimeError('Old-format transaction requires manual inspection before recovery')
    data['active'], data['previous'] = target, old if old in IPS else None
    for color in (old, target):
        if color not in IPS: continue
        images = transaction_images(transaction, color)
        status = 'active' if color == target else 'retained'
        if color in transaction.get('isolated', {}):
            status = 'isolated'
            if color == old: data['previous'] = None
        data['slots'][color] = {'status': status, 'images': images, 'release': release_id(images)}
    atomic(HOME / 'control/active', target + '\n', 0o644)
    save(data)
    record('recovered_committed_switch', active=target)
    (HOME / 'transaction.json').unlink()
    print('Finalized healthy target:', target)

def abandon_transaction(transaction, force):
    if not force:
        raise RuntimeError('Abandoning an unavailable release requires --abandon --force')
    for color in (transaction['from'], transaction['to']):
        if color in IPS:
            quarantine_backend(transaction, color)
    actual_route = route_color()
    if actual_route not in (transaction['from'], transaction['to']):
        raise RuntimeError('Nginx route is outside this transaction; inspect manually')
    data = json.loads(json.dumps(transaction.get('snapshot')))
    if data is None:
        raise RuntimeError('Old-format transaction requires manual inspection before recovery')
    for color in transaction.get('isolated', {}):
        data['slots'][color]['status'] = 'isolated'
    data['active'], data['previous'] = actual_route, None
    atomic(HOME / 'control/active', actual_route + '\n', 0o644)
    save(data)
    archive = HOME / 'abandoned-transactions' / (str(time.time_ns()) + '.json')
    atomic(archive, json.dumps(transaction, indent=2) + '\n')
    record('transaction_abandoned_for_forward_fix', active=actual_route, journal=str(archive))
    (HOME / 'transaction.json').unlink()
    print('Backends isolated; transaction archived. Service health is NOT restored; manual forward-fix is now permitted.')

def recover(force=False, target=None, abandon=False):
    transaction_path = HOME / 'transaction.json'
    if not transaction_path.exists():
        print('No unfinished transaction'); return
    transaction = json.loads(transaction_path.read_text())
    if abandon:
        if target is not None:
            raise RuntimeError('--target and --abandon cannot be combined')
        abandon_transaction(transaction, force)
        return
    if target is not None:
        if target not in (transaction['from'], transaction['to']):
            raise RuntimeError('Recovery target must belong to the unfinished transaction')
        if target == transaction['from']:
            restore_previous(transaction, allow_isolated=force)
            print('Recovered previous environment:', target)
        else:
            finish_target(transaction, force)
        return
    target = transaction['to']
    data = state()
    # A process may die after state commit but before unlink. Finalize the
    # committed target instead of undoing a successful deployment.
    committed = (data['active'] == target and route_color() == target and
                 (HOME / 'control/active').read_text().strip() == target)
    if committed:
        finish_target(transaction, force)
    else:
        restore_previous(transaction)
        print('Recovered previous environment:', transaction['from'])

def rollback(expected_release, force=False):
    data = state()
    assert_consistent(data)
    previous = data.get('previous')
    if previous not in IPS:
        raise RuntimeError('No retained rollback release is available')
    slot = verify_slot(previous, {'retained'})
    if not expected_release or expected_release != slot.get('release'):
        raise RuntimeError('Rollback requires the exact retained release ID shown by status')
    print('Rollback release:', slot['release'], json.dumps(slot['images'], sort_keys=True))
    if force: switch(previous, force=True)
    else: switch(previous)

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
    output['deploymentLockOwnerAdvisory'] = None
    with contextlib.suppress(OSError, ValueError):
        output['deploymentLockOwnerAdvisory'] = json.loads((HOME / 'lock-owner.json').read_text())
    print(json.dumps(output, indent=2))

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lock-timeout', type=lock_timeout,
                        help='Maximum seconds to wait for the deployment lock (0..14400; deploy default 14400, other commands 600)')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('init'); sub.add_parser('status'); sub.add_parser('finish-bootstrap')
    p = sub.add_parser('legacy-zone-evidence', help='Inspect current Legacy JVM evidence without preparing a slot')
    p.add_argument('--backend-time-zone', help='Reviewed requested zone, or reuse the existing immutable zone pin')
    p.add_argument('--legacy-probe-timeout', type=parse_legacy_probe_timeout,
                   default=DEFAULT_LEGACY_PROBE_TIMEOUT,
                   help='Legacy evidence probe timeout in seconds (6..60; default 10)')
    p.add_argument('--legacy-probe-attempts', type=parse_legacy_probe_attempts,
                   default=DEFAULT_LEGACY_PROBE_ATTEMPTS,
                   help='Legacy evidence probe attempts (1..3; default 2)')
    for verb in ('prepare', 'switch', 'check'):
        p = sub.add_parser(verb); p.add_argument('color', choices=tuple(IPS))
        if verb == 'prepare':
            for app in APPS:
                p.add_argument('--' + app + '-image')
                p.add_argument('--' + app + '-env-file')
            p.add_argument('--replace-env', action='store_true')
            p.add_argument('--pending-recovery-not-before')
            p.add_argument('--backend-time-zone')
            p.add_argument('--confirm-legacy-time-zone', metavar='CONTAINER_ID')
            p.add_argument('--confirm-legacy-time-zone-evidence', type=legacy_confirmation_evidence, metavar='SHA256',
                           help='Fingerprint of the displayed evidence reviewed for this exact Legacy JVM')
            p.add_argument('--legacy-probe-timeout', type=parse_legacy_probe_timeout,
                           default=DEFAULT_LEGACY_PROBE_TIMEOUT,
                           help='Legacy preparation probe timeout in seconds (6..60; default 10)')
            p.add_argument('--legacy-probe-attempts', type=parse_legacy_probe_attempts,
                           default=DEFAULT_LEGACY_PROBE_ATTEMPTS,
                           help='Legacy preparation probe attempts (1..3; default 2)')
            p.add_argument('--force', action='store_true')
        if verb == 'switch': p.add_argument('--force', action='store_true')
    p = sub.add_parser('rollback'); p.add_argument('--release', required=True)
    p.add_argument('--force', action='store_true')
    p = sub.add_parser('recover'); p.add_argument('--force', action='store_true')
    p.add_argument('--target', choices=tuple(IPS)); p.add_argument('--abandon', action='store_true')
    p = sub.add_parser('deploy'); p.add_argument('--service', choices=APPS, required=True)
    p.add_argument('--image', required=True)
    p.add_argument('--env-file')
    p.add_argument('--replace-env', action='store_true')
    p.add_argument('--pending-recovery-not-before')
    p.add_argument('--backend-time-zone')
    p.add_argument('--confirm-legacy-time-zone', metavar='CONTAINER_ID')
    p.add_argument('--job-deadline-epoch', type=job_deadline,
                   help='Conservative CI cancellation deadline; keep 9000 seconds after lock acquisition')
    p.add_argument('--force', action='store_true')
    args = parser.parse_args()
    if args.command == 'legacy-zone-evidence':
        return run_legacy_zone_evidence(args)
    HOME.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.command == 'status': status(); return
    wait_seconds = default_lock_timeout(args.command) if args.lock_timeout is None else args.lock_timeout
    deadline = args.job_deadline_epoch if args.command == 'deploy' else None
    with deployment_lock(wait_seconds, args.command, deadline):
        if (HOME / 'transaction.json').exists() and args.command not in ('recover', 'check'):
            raise RuntimeError('Interrupted switch detected; run recover or inspect status first')
        if args.command == 'recover':
            recover(force=args.force, target=args.target, abandon=args.abandon)
        elif args.command == 'init': init()
        elif args.command == 'prepare':
            prepare(args.color, {a: getattr(args, a + '_image') for a in APPS if getattr(args, a + '_image')},
                {a: getattr(args, a + '_env_file') for a in APPS if getattr(args, a + '_env_file')},
                replace_env=args.replace_env, recovery_not_before=args.pending_recovery_not_before, force=args.force,
                backend_zone=args.backend_time_zone, legacy_zone_confirmation=args.confirm_legacy_time_zone,
                legacy_zone_evidence=args.confirm_legacy_time_zone_evidence,
                legacy_probe_timeout=args.legacy_probe_timeout, legacy_probe_attempts=args.legacy_probe_attempts)
        elif args.command == 'switch': switch(args.color, force=args.force)
        elif args.command == 'check': print(json.dumps(check(args.color), indent=2))
        elif args.command == 'finish-bootstrap': finish_bootstrap()
        elif args.command == 'rollback':
            rollback(args.release, force=args.force)
        elif args.command == 'deploy':
            active = state()['active']
            if active not in IPS: raise RuntimeError('Finish bootstrap before automatic deployments')
            target = 'green' if active == 'blue' else 'blue'
            prepare(target, {args.service: args.image}, {args.service: args.env_file} if args.env_file else None,
                replace_env=args.replace_env, recovery_not_before=args.pending_recovery_not_before, force=args.force,
                backend_zone=args.backend_time_zone, legacy_zone_confirmation=args.confirm_legacy_time_zone)
            # Keep the healthy candidate inactive if preparation exceeded its expected time budget.
            if deadline is not None and deadline - time.time() < SWITCH_RESERVE_SECONDS:
                raise RuntimeError('Insufficient job time remains to switch; active route is unchanged. Inspect the inactive candidate and retry')
            switch(target, force=args.force)

if __name__ == '__main__':
    try: sys.exit(main())
    except Exception as error:
        print(f'Deployment stopped safely: {error}', file=sys.stderr)
        sys.exit(1)
