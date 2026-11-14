#!/usr/bin/env python3
"""DEV_COM_ configuration for existing shared infrastructure services.

Default: validate Secrets locally. --deploy performs a volume-preserving update
only after the shared network, running container and named volumes match.
This is not a database/network bootstrap or destructive reset tool.
"""

import argparse
import contextlib
import fcntl
import ipaddress
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[2]
ENV_HOME = Path('/home/kwung/deploy/ieum-infra')
LOCK_FILE = Path('/home/kwung/deploy/bluegreen/deploy.lock')
TRANSPORT = 'IEUM_INFRA_SECRETS_JSON'
PREFIX = 'DEV_COM_'
MAX_BYTES = 96 * 1024
FORBIDDEN = '\x00\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029'
COMMON = ('DATA_DOCKER_NETWORK', 'DATA_DOCKER_NETWORK_SUBNET', 'HOST_BIND_IP')
SCHEMAS = {
    'postgres': ('POSTGRES_ENV_FILE', 'POSTGRES_CONTAINER_NAME', 'POSTGRES_IP',
                 'POSTGRES_PORT', 'POSTGRES_HOST_PORT', 'POSTGRES_DB', 'POSTGRES_USER', 'POSTGRES_PASSWORD'),
    'mongo': ('MONGO_ENV_FILE', 'MONGO_CONTAINER_NAME', 'MONGO_IP', 'MONGO_PORT',
              'MONGO_HOST_PORT', 'MONGO_INITDB_DATABASE', 'MONGO_INITDB_ROOT_USERNAME',
              'MONGO_INITDB_ROOT_PASSWORD'),
    'redis': ('REDIS_ENV_FILE', 'REDIS_CONTAINER_NAME', 'REDIS_IP'),
    'phoenix': ('PHOENIX_ENV_FILE', 'PHOENIX_CONTAINER_NAME', 'PHOENIX_UI_PORT'),
    'notion-mcp': ('NOTION_MCP_ENV_FILE', 'NOTION_MCP_CONTAINER_NAME',
                   'NOTION_MCP_PORT', 'NOTION_MCP_AUTH_TOKEN'),
}
OPTIONALS = {'redis': {'REDIS_HOST_PORT': '6379'},
             'notion-mcp': {'NOTION_MCP_HOST_PORT': '3000'}}
VOLUMES = {'postgres': ('ieum_postgres_data', '/var/lib/postgresql/data'),
           'mongo': ('ieum_mongo_data', '/data/db'), 'redis': ('ieum_redis_data', '/data'),
           'phoenix': ('ieum_phoenix_data', '/mnt/data')}
REQUIRED_SECRETS = frozenset(PREFIX + key for key in COMMON + tuple(
    key for schema in SCHEMAS.values() for key in schema))
OPTIONAL_SECRETS = frozenset(PREFIX + key for defaults in OPTIONALS.values() for key in defaults)


class ConfigurationError(ValueError):
    pass


def unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ConfigurationError('duplicate_secret_name')
        result[name] = value
    return result


def invalid_constant(_value):
    raise ConfigurationError('invalid_json')


def main_profile_module():
    path = ROOT / 'scripts' / 'main' / 'infra_env.py'
    spec = importlib.util.spec_from_file_location('ieum_main_infra', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_settings(payload, service, profile='dev', limits_payload=None):
    if profile == 'main':
        return main_profile_module().build_settings(payload, service, limits_payload)
    if profile != 'dev':
        raise ConfigurationError('unknown_profile')
    if service not in SCHEMAS:
        raise ConfigurationError('unknown_service')
    if not isinstance(payload, str) or not payload:
        raise ConfigurationError('missing_configuration')
    try:
        if len(payload.encode('utf-8')) > MAX_BYTES:
            raise ConfigurationError('secrets_payload_too_large')
        source = json.loads(payload, object_pairs_hook=unique_object, parse_constant=invalid_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ConfigurationError('invalid_json') from None
    if not isinstance(source, dict):
        raise ConfigurationError('invalid_json')
    wanted = COMMON + SCHEMAS[service] + tuple(OPTIONALS.get(service, {}))
    selected = {}
    for name, value in source.items():
        normalized = name.upper()
        if not normalized.startswith(PREFIX) or normalized[len(PREFIX):] not in wanted:
            continue
        if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name) is None:
            raise ConfigurationError('invalid_secret_name')
        key = normalized[len(PREFIX):]
        if key in selected:
            raise ConfigurationError('duplicate_configuration_key')
        if not isinstance(value, str) or any(character in value for character in FORBIDDEN):
            raise ConfigurationError('invalid_single_line_value')
        if '__FILL_' in value or re.search(r'<[^>]+>', value):
            raise ConfigurationError('placeholder_configuration')
        selected[key] = value
    selected.setdefault('HOST_BIND_IP', '127.0.0.1')
    if selected['HOST_BIND_IP'] == '':
        selected['HOST_BIND_IP'] = '127.0.0.1'
    for key, default in OPTIONALS.get(service, {}).items():
        if not selected.get(key):
            selected[key] = default
    if any(not selected.get(key, '').strip() for key in COMMON + SCHEMAS[service]):
        raise ConfigurationError('missing_required_configuration')
    if selected['HOST_BIND_IP'] != '127.0.0.1':
        raise ConfigurationError('dev_requires_localhost_binding')
    network = selected.pop('DATA_DOCKER_NETWORK')
    if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', network) is None or network in (
            'host', 'bridge', 'none', 'ieum_net_blue', 'ieum_net_green'):
        raise ConfigurationError('invalid_data_network')
    try:
        subnet = ipaddress.ip_network(selected.pop('DATA_DOCKER_NETWORK_SUBNET'), strict=True)
    except ValueError:
        raise ConfigurationError('invalid_data_subnet') from None
    if subnet.version != 4 or not any(subnet.subnet_of(ipaddress.ip_network(cidr))
                                     for cidr in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')):
        raise ConfigurationError('invalid_data_subnet')
    selected['GREEN_NETWORK'] = network
    selected['GREEN_NETWORK_SUBNET'] = str(subnet)
    prefix = service.replace('-', '_').upper()
    path = Path(selected.pop(prefix + '_ENV_FILE'))
    if path.parent != ENV_HOME or re.fullmatch(re.escape(service) + r'(?:[.-][A-Za-z0-9_-]+)*\.env', path.name) is None:
        raise ConfigurationError('unsafe_env_path')
    name = selected[prefix + '_CONTAINER_NAME']
    if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', name) is None:
        raise ConfigurationError('invalid_container_name')
    if prefix + '_IP' in selected:
        try:
            address = ipaddress.ip_address(selected[prefix + '_IP'])
        except ValueError:
            raise ConfigurationError('invalid_service_ip') from None
        if address not in subnet or address in (subnet.network_address, subnet.broadcast_address):
            raise ConfigurationError('invalid_service_ip')
    for key, value in selected.items():
        if key.endswith('_PORT') and (re.fullmatch(r'[1-9][0-9]{0,4}', value) is None or int(value) > 65535):
            raise ConfigurationError('invalid_port')
    if selected.get('POSTGRES_PORT', '5432') != '5432' or selected.get('MONGO_PORT', '27017') != '27017':
        raise ConfigurationError('fixed_container_port')
    return {'service': service, 'path': path, 'values': selected,
            'name': name, 'project': 'ieum-green-' + service}


def compose_environment(values):
    # Compose double-quoted dotenv syntax; dollars are literal, not substitutions.
    def quote(value):
        return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('$', '$$') + '"'
    return ''.join(key + '=' + quote(value) + '\n' for key, value in sorted(values.items())).encode('utf-8')


def command_environment():
    allowed = ('PATH', 'HOME', 'LANG', 'LC_ALL', 'DOCKER_HOST', 'DOCKER_CONTEXT',
               'DOCKER_CONFIG', 'DOCKER_CERT_PATH', 'DOCKER_TLS_VERIFY', 'SSH_AUTH_SOCK')
    return {**{key: os.environ[key] for key in allowed if key in os.environ},
            'COMPOSE_DISABLE_ENV_FILE': '1'}


def run(arguments, timeout=30):
    try:
        result = subprocess.run(arguments, env=command_environment(), capture_output=True,
                                text=True, timeout=timeout, check=True)
        return result.stdout
    except (OSError, subprocess.SubprocessError):
        raise ConfigurationError('docker_command_failed') from None


def inspect(kind, name):
    try:
        result = json.loads(run(['docker', kind, 'inspect', name] if kind != 'container'
                                else ['docker', 'inspect', name]))
        if not isinstance(result, list) or len(result) != 1:
            raise ValueError()
        return result[0]
    except (ValueError, KeyError, TypeError):
        raise ConfigurationError('invalid_docker_inspection') from None


def validate_live(settings):
    values = settings['values']
    network = inspect('network', values['GREEN_NETWORK'])
    subnets = [entry.get('Subnet') for entry in network.get('IPAM', {}).get('Config', [])]
    if network.get('Driver') != 'bridge' or network.get('Internal') or subnets != [values['GREEN_NETWORK_SUBNET']]:
        raise ConfigurationError('data_network_not_prepared')
    container = inspect('container', settings['name'])
    if container.get('Name') != '/' + settings['name'] or not container.get('State', {}).get('Running'):
        raise ConfigurationError('existing_running_container_required')
    networks = container.get('NetworkSettings', {}).get('Networks', {})
    if set(networks) != {values['GREEN_NETWORK']}:
        raise ConfigurationError('finish_network_migration_before_deploy')
    prefix = settings['service'].replace('-', '_').upper()
    if prefix + '_IP' in values:
        endpoint = networks[values['GREEN_NETWORK']]
        if endpoint.get('IPAddress') != values[prefix + '_IP'] or endpoint.get('IPAddress') == network['IPAM']['Config'][0].get('Gateway'):
            raise ConfigurationError('existing_data_ip_mismatch')
    project = (container.get('Config', {}).get('Labels') or {}).get('com.docker.compose.project')
    if project != settings['project']:
        raise ConfigurationError('existing_compose_project_mismatch')
    if settings['service'] in VOLUMES:
        logical, destination = VOLUMES[settings['service']]
        mounts = [mount for mount in container.get('Mounts', []) if mount.get('Destination') == destination]
        if len(mounts) != 1 or mounts[0].get('Type') != 'volume' or mounts[0].get('Name') != settings['project'] + '_' + logical:
            raise ConfigurationError('existing_volume_identity_mismatch')


@contextlib.contextmanager
def deployment_lock(path=None):
    descriptor = os.open(LOCK_FILE if path is None else path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'r+') as lock:
        metadata = os.fstat(lock.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
            raise ConfigurationError('unsafe_deployment_lock')
        deadline = time.monotonic() + 300
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ConfigurationError('deployment_lock_busy') from None
                time.sleep(0.25)
        yield


def existing_environment(path, directory=None):
    metadata = (ENV_HOME if directory is None else directory).lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise ConfigurationError('private_env_directory_required')
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, 'rb') as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1:
            raise ConfigurationError('unsafe_existing_env_file')
        content = source.read(MAX_BYTES + 1)
        if len(content) > MAX_BYTES:
            raise ConfigurationError('existing_env_file_too_large')
        return content


def atomic_environment(path, content, directory=None):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=ENV_HOME if directory is None else directory, prefix='.infra-env-', delete=False) as output:
            temporary = Path(output.name)
            os.fchmod(output.fileno(), 0o600)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def deploy(settings):
    with deployment_lock():
        validate_live(settings)
        path = settings['path']
        previous = existing_environment(path)
        compose = ['docker', 'compose', '--project-name', settings['project'],
                   '-f', str(ROOT / 'compose' / (settings['service'] + '.green.compose.yml')),
                   '--env-file', str(path)]
        atomic_environment(path, compose_environment(settings['values']))
        try:
            run(compose + ['config', '--quiet'])
            arguments = compose + ['up', '-d', '--no-deps', '--pull', 'never']
            if settings['service'] == 'notion-mcp':
                arguments.append('--build')
            arguments.append(settings['service'])
            run(arguments, timeout=600)
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                container = inspect('container', settings['name'])
                state = container.get('State', {})
                if state.get('Running') and (state.get('Health', {}).get('Status') == 'healthy'
                        or settings['service'] == 'notion-mcp'):
                    validate_live(settings)
                    return
                time.sleep(2)
            raise ConfigurationError('service_health_timeout')
        except Exception:
            # Config restoration does not undo any already-completed container recreation.
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                atomic_environment(path, previous)
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', choices=('dev', 'main'), default='dev')
    parser.add_argument('--service', choices=SCHEMAS, required=True)
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument('--deploy', action='store_true')
    operation.add_argument('--bootstrap', action='store_true')
    arguments = parser.parse_args(argv)
    payload = os.environ.pop(TRANSPORT, None)
    if arguments.profile == 'main':
        main_profile = main_profile_module()
        return main_profile.run_operation(
            main_profile.consume_explicit_secrets(os.environ, arguments.service), arguments.service,
            limits_payload=os.environ.pop('IEUM_INFRA_LIMITS_JSON', None),
            enabled=os.environ.pop('MAIN_DEPLOY_ENABLED', None),
            bootstrap_enabled=os.environ.pop('MAIN_BOOTSTRAP_ENABLED', None),
            operation='bootstrap' if arguments.bootstrap else 'deploy' if arguments.deploy else 'check')
    if arguments.bootstrap:
        parser.error('--bootstrap is only available for the explicit main profile')
    try:
        settings = build_settings(payload, arguments.service)
        if arguments.deploy:
            deploy(settings)
        print('DEV infrastructure configuration accepted: ' + arguments.service)
        return 0
    except ConfigurationError as failure:
        print('DEV infrastructure operation rejected: ' + str(failure) +
              '. Container changes may be partial if deployment had already started.', file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, KeyError):
        print('DEV infrastructure operation failed; inspect network/project/volume/configuration preparation. '
              'Container changes may be partial if deployment had already started.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
