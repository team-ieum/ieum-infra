#!/usr/bin/env python3
"""Isolated MAIN shared services; explicit fresh bootstrap or volume-preserving update.

Invoked through scripts/dev/infra_env.py --profile main. No network is created,
no DEV resource is adopted, and no volume/container is deleted on failure.
"""

import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import sys
import time


ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location('main_infra_shared', ROOT / 'scripts/dev/infra_env.py')
shared = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(shared)
ConfigurationError = shared.ConfigurationError
ENV_HOME = Path('/home/kwung/deploy/ieum-infra-main')
LOCK_FILE = Path('/home/kwung/deploy/bluegreen-main/deploy.lock')
PREFIX = 'MAIN_COM_'
NETWORK = 'ieum_main_net_data'
ENVIRONMENT_LABEL = 'ieum.bluegreen.environment'
SERVICE_LABEL = 'ieum.infra.service'
DEV_HOST_PORTS = frozenset((5432, 27017, 6379, 6006, 3000))
RESOURCE_TRANSPORT = 'IEUM_INFRA_LIMITS_JSON'
OPTIONALS = {'redis': {'REDIS_HOST_PORT': '16379'},
             'notion-mcp': {'NOTION_MCP_HOST_PORT': '13000'}}


def parse_object(payload):
    if not isinstance(payload, str) or not payload:
        raise ConfigurationError('missing_configuration')
    try:
        if len(payload.encode('utf-8')) > shared.MAX_BYTES:
            raise ConfigurationError('configuration_payload_too_large')
        result = json.loads(payload, object_pairs_hook=shared.unique_object,
                            parse_constant=shared.invalid_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ConfigurationError('invalid_json') from None
    if not isinstance(result, dict):
        raise ConfigurationError('invalid_json')
    return result


def resource_settings(payload, service):
    source = parse_object(payload)
    prefix = service.replace('-', '_').upper()
    keys = {'SERVICE_CPUS': 'MAIN_' + prefix + '_CPUS',
            'SERVICE_MEMORY': 'MAIN_' + prefix + '_MEMORY',
            'INFRA_LOG_MAX_SIZE': 'MAIN_INFRA_LOG_MAX_SIZE',
            'INFRA_LOG_MAX_FILES': 'MAIN_INFRA_LOG_MAX_FILES'}
    result = {}
    for target, name in keys.items():
        value = source.get(name)
        if not isinstance(value, str) or not value or value != value.strip():
            raise ConfigurationError('main_resource_limits_required')
        result[target] = value
    if not re.fullmatch(r'(?:[1-9][0-9]*(?:\.[0-9]+)?|0\.[0-9]*[1-9][0-9]*)', result['SERVICE_CPUS']):
        raise ConfigurationError('invalid_main_cpu_limit')
    if any(not re.fullmatch(r'[1-9][0-9]*[kKmMgG]', result[key])
           for key in ('SERVICE_MEMORY', 'INFRA_LOG_MAX_SIZE')):
        raise ConfigurationError('invalid_main_memory_or_log_limit')
    if not re.fullmatch(r'[1-9][0-9]{0,2}', result['INFRA_LOG_MAX_FILES']):
        raise ConfigurationError('invalid_main_log_file_limit')
    if service == 'notion-mcp':
        version = source.get('MAIN_NOTION_MCP_VERSION')
        part = r'(?:0|[1-9][0-9]*)'
        if not isinstance(version, str) or not re.fullmatch(part + r'\.' + part + r'\.' + part, version):
            raise ConfigurationError('exact_main_notion_mcp_version_required')
        result['NOTION_MCP_VERSION'] = version
    return result


def build_settings(payload, service, limits_payload):
    if service not in shared.SCHEMAS:
        raise ConfigurationError('unknown_service')
    source = parse_object(payload)
    wanted = shared.COMMON + shared.SCHEMAS[service] + tuple(OPTIONALS.get(service, {}))
    selected = {}
    for name, value in source.items():
        normalized = name.upper()
        if not normalized.startswith(PREFIX) or normalized[len(PREFIX):] not in wanted:
            continue
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
            raise ConfigurationError('invalid_secret_name')
        key = normalized[len(PREFIX):]
        if key in selected:
            raise ConfigurationError('duplicate_configuration_key')
        if not isinstance(value, str) or any(c in value for c in shared.FORBIDDEN):
            raise ConfigurationError('invalid_single_line_value')
        if '__FILL_' in value or re.search(r'<[^>]+>', value):
            raise ConfigurationError('placeholder_configuration')
        selected[key] = value
    if not selected.get('HOST_BIND_IP'):
        selected['HOST_BIND_IP'] = '127.0.0.1'
    for key, default in OPTIONALS.get(service, {}).items():
        if not selected.get(key):
            selected[key] = default
    if any(not selected.get(key, '').strip() for key in shared.COMMON + shared.SCHEMAS[service]):
        raise ConfigurationError('missing_required_configuration')
    if selected['HOST_BIND_IP'] != '127.0.0.1':
        raise ConfigurationError('main_requires_localhost_binding')
    if selected.pop('DATA_DOCKER_NETWORK') != NETWORK:
        raise ConfigurationError('main_requires_isolated_data_network')
    try:
        subnet = ipaddress.ip_network(selected.pop('DATA_DOCKER_NETWORK_SUBNET'), strict=True)
    except ValueError:
        raise ConfigurationError('invalid_data_subnet') from None
    if subnet.version != 4 or not any(subnet.subnet_of(ipaddress.ip_network(cidr))
                                     for cidr in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')):
        raise ConfigurationError('invalid_data_subnet')
    # Logical Compose compatibility only; the actual network is MAIN-specific.
    selected['GREEN_NETWORK'] = NETWORK
    selected['GREEN_NETWORK_SUBNET'] = str(subnet)
    prefix = service.replace('-', '_').upper()
    raw_path = selected.pop(prefix + '_ENV_FILE')
    path = Path(raw_path)
    if str(path) != raw_path or path.parent != ENV_HOME or not re.fullmatch(
            re.escape(service) + r'(?:[.-][A-Za-z0-9_-]+)*\.env', path.name):
        raise ConfigurationError('unsafe_main_env_path')
    name = selected[prefix + '_CONTAINER_NAME']
    if name != 'ieum-main-' + service:
        raise ConfigurationError('main_container_identity_required')
    if prefix + '_IP' in selected:
        try:
            address = ipaddress.ip_address(selected[prefix + '_IP'])
        except ValueError:
            raise ConfigurationError('invalid_service_ip') from None
        if address not in subnet or address in (subnet.network_address, subnet.broadcast_address):
            raise ConfigurationError('invalid_service_ip')
    for key, value in selected.items():
        if key.endswith('_PORT'):
            if not re.fullmatch(r'[1-9][0-9]{0,4}', value) or int(value) > 65535:
                raise ConfigurationError('invalid_port')
            if (key.endswith('_HOST_PORT') or key == 'PHOENIX_UI_PORT') and int(value) in DEV_HOST_PORTS:
                raise ConfigurationError('main_host_port_must_differ_from_dev_defaults')
    if selected.get('POSTGRES_PORT', '5432') != '5432' or selected.get('MONGO_PORT', '27017') != '27017':
        raise ConfigurationError('fixed_container_port')
    selected.update(resource_settings(limits_payload, service))
    return {'profile': 'main', 'service': service, 'path': path, 'values': selected,
            'name': name, 'project': 'ieum-main-' + service}


def run(arguments, timeout=30):
    return shared.run(arguments, timeout=timeout)


def inspect(kind, name):
    try:
        data = json.loads(run(['docker', kind, 'inspect', name] if kind != 'container'
                              else ['docker', 'inspect', name]))
        if not isinstance(data, list) or len(data) != 1:
            raise ValueError()
        return data[0]
    except (ValueError, TypeError):
        raise ConfigurationError('invalid_docker_inspection') from None


def validate_network(settings):
    values = settings['values']
    network = inspect('network', NETWORK)
    config = network.get('IPAM', {}).get('Config', [])
    if network.get('Name') != NETWORK or network.get('Driver') != 'bridge' or network.get('Internal') is not False \
            or len(config) != 1 or config[0].get('Subnet') != values['GREEN_NETWORK_SUBNET'] \
            or (network.get('Labels') or {}).get(ENVIRONMENT_LABEL) != 'main':
        raise ConfigurationError('owned_main_data_network_required')
    prefix = settings['service'].replace('-', '_').upper()
    if values.get(prefix + '_IP') == config[0].get('Gateway'):
        raise ConfigurationError('service_ip_is_gateway')
    return network


def expected_host_port(settings):
    prefix = settings['service'].replace('-', '_').upper()
    return settings['values'].get(prefix + '_HOST_PORT', settings['values'].get('PHOENIX_UI_PORT'))


def validate_live(settings):
    validate_network(settings)
    container = inspect('container', settings['name'])
    labels = container.get('Config', {}).get('Labels') or {}
    if container.get('Name') != '/' + settings['name'] or not container.get('State', {}).get('Running') \
            or labels.get('com.docker.compose.project') != settings['project'] \
            or labels.get(ENVIRONMENT_LABEL) != 'main' or labels.get(SERVICE_LABEL) != settings['service']:
        raise ConfigurationError('owned_running_main_service_required')
    networks = container.get('NetworkSettings', {}).get('Networks', {})
    if set(networks) != {NETWORK}:
        raise ConfigurationError('main_data_network_only')
    prefix = settings['service'].replace('-', '_').upper()
    if prefix + '_IP' in settings['values'] and networks[NETWORK].get('IPAddress') != settings['values'][prefix + '_IP']:
        raise ConfigurationError('existing_main_ip_mismatch')
    bindings = container.get('HostConfig', {}).get('PortBindings') or {}
    published = [binding for group in bindings.values() for binding in (group or [])]
    if len(published) != 1 or published[0] != {'HostIp': '127.0.0.1', 'HostPort': expected_host_port(settings)}:
        raise ConfigurationError('existing_main_port_binding_mismatch')
    if settings['service'] in shared.VOLUMES:
        logical, destination = shared.VOLUMES[settings['service']]
        actual_name = settings['project'] + '_' + logical
        mounts = [m for m in container.get('Mounts', []) if m.get('Destination') == destination]
        if len(mounts) != 1 or mounts[0].get('Type') != 'volume' or mounts[0].get('Name') != actual_name:
            raise ConfigurationError('existing_main_volume_identity_mismatch')
        volume = inspect('volume', actual_name)
        volume_labels = volume.get('Labels') or {}
        if volume_labels.get('com.docker.compose.project') != settings['project'] \
                or volume_labels.get(ENVIRONMENT_LABEL) != 'main':
            raise ConfigurationError('owned_main_volume_required')


def bootstrap_preflight(settings):
    validate_network(settings)
    # Listing must succeed. A daemon/permission failure is never evidence of absence.
    names = set(run(['docker', 'ps', '--all', '--format', '{{.Names}}']).splitlines())
    if settings['name'] in names:
        raise ConfigurationError('main_bootstrap_container_already_exists')
    project_names = run(['docker', 'ps', '--all', '--filter',
                         'label=com.docker.compose.project=' + settings['project'], '--format', '{{.Names}}'])
    volumes = run(['docker', 'volume', 'ls', '--format', '{{.Name}}']).splitlines()
    if project_names.strip() or any(name.startswith(settings['project'] + '_') for name in volumes):
        raise ConfigurationError('main_bootstrap_project_or_volume_already_exists')
    if shared.existing_environment(settings['path'], directory=ENV_HOME) is not None:
        raise ConfigurationError('main_bootstrap_env_already_exists')
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(('127.0.0.1', int(expected_host_port(settings))))
        except OSError:
            raise ConfigurationError('main_host_port_unavailable') from None


def deploy(settings, bootstrap=False):
    with shared.deployment_lock(LOCK_FILE):
        if bootstrap:
            bootstrap_preflight(settings)
        else:
            validate_live(settings)
        path = settings['path']
        previous = shared.existing_environment(path, directory=ENV_HOME)
        compose = ['docker', 'compose', '--project-name', settings['project'],
                   '-f', str(ROOT / 'compose' / (settings['service'] + '.main.compose.yml')),
                   '--env-file', str(path)]
        shared.atomic_environment(path, shared.compose_environment(settings['values']), directory=ENV_HOME)
        try:
            run(compose + ['config', '--quiet'])
            arguments = compose + ['up', '-d', '--no-deps', '--pull', 'missing' if bootstrap else 'never']
            if settings['service'] == 'notion-mcp':
                arguments.append('--build')
            arguments.append(settings['service'])
            run(arguments, timeout=600)
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                container = inspect('container', settings['name'])
                if container.get('State', {}).get('Running') \
                        and container.get('State', {}).get('Health', {}).get('Status') == 'healthy':
                    validate_live(settings)
                    return
                time.sleep(2)
            raise ConfigurationError('main_service_health_timeout')
        except Exception:
            # Retain all newly created containers/volumes for inspection; never silently re-bootstrap.
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                shared.atomic_environment(path, previous, directory=ENV_HOME)
            raise


def run_operation(payload, service, *, limits_payload, enabled, operation, bootstrap_enabled=None):
    try:
        settings = build_settings(payload, service, limits_payload)
        if operation not in ('check', 'deploy', 'bootstrap'):
            raise ConfigurationError('unknown_operation')
        if operation != 'check':
            if operation == 'bootstrap' and bootstrap_enabled != 'true':
                raise ConfigurationError('main_bootstrap_disabled')
            if operation == 'deploy' and enabled != 'true':
                raise ConfigurationError('main_deploy_disabled')
            deploy(settings, bootstrap=operation == 'bootstrap')
        print('MAIN infrastructure configuration accepted: ' + service + ' (' + operation + ')')
        return 0
    except ConfigurationError as failure:
        print('MAIN infrastructure operation rejected: ' + str(failure) +
              '. Existing/new containers and volumes are retained; changes may be partial.', file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, KeyError):
        print('MAIN infrastructure operation failed; inspect private configuration and MAIN resource ownership. '
              'Existing/new containers and volumes are retained; changes may be partial.', file=sys.stderr)
        return 1
