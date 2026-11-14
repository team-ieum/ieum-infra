#!/usr/bin/env python3
"""Plan MAIN-only networks and private directories; --apply explicitly provisions.

No DEV resource, Nginx route, application state or database is modified. Existing
MAIN resources are reused only after ownership and exact configuration checks.
"""

import argparse
import fcntl
import ipaddress
import json
import math
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys


DEPLOY_HOME = Path('/home/kwung/deploy/bluegreen-main')
INFRA_HOME = Path('/home/kwung/deploy/ieum-infra-main')
EXPECTED_NAMES = {'blue': 'ieum_main_net_blue', 'green': 'ieum_main_net_green', 'data': 'ieum_main_net_data'}
ENVIRONMENT_LABEL = 'ieum.bluegreen.environment'
MAX_INPUT_BYTES = 16384


class PreparationError(ValueError):
    pass


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PreparationError('duplicate_json_key')
        result[key] = value
    return result


def reject_constant(_value):
    raise PreparationError('nonfinite_json_value')


def read_json(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise PreparationError('unsafe_input_file')
        raw = source.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise PreparationError('input_too_large')
    try:
        return json.loads(raw, object_pairs_hook=unique_object, parse_constant=reject_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise PreparationError('invalid_json') from None


def validate_topology(value):
    if not isinstance(value, dict) or set(value) != {'version', 'data', 'blue', 'green'} \
            or type(value['version']) is not int or value['version'] != 2:
        raise PreparationError('main_topology_version_2_required')
    networks = {}
    for role, name in EXPECTED_NAMES.items():
        slot = value.get(role)
        if not isinstance(slot, dict) or slot.get('network') != name:
            raise PreparationError('main_network_name_required')
        expected_keys = {'network', 'subnet', 'stores'} if role == 'data' else {
            'network', 'subnet', 'containerName', 'ip', 'backendIp', 'agentIp', 'envDir', 'envFile'}
        if set(slot) != expected_keys:
            raise PreparationError('invalid_main_topology_fields')
        try:
            subnet = ipaddress.ip_network(slot.get('subnet'), strict=True)
        except (ValueError, TypeError):
            raise PreparationError('invalid_main_subnet') from None
        if subnet.version != 4 or str(subnet) != slot['subnet'] or not any(
                subnet.subnet_of(ipaddress.ip_network(block))
                for block in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')):
            raise PreparationError('private_main_ipv4_subnet_required')
        if any(subnet.overlaps(other) for other in networks.values()):
            raise PreparationError('main_subnets_overlap')
        networks[role] = subnet
    stores = value['data'].get('stores')
    if stores != {service: 'ieum-main-' + service for service in ('postgres', 'redis', 'mongo')}:
        raise PreparationError('main_data_store_names_required')
    seen = set()
    for color in ('blue', 'green'):
        slot = value[color]
        if slot.get('containerName') != 'ieum-main-' + color + '-frontend':
            raise PreparationError('main_frontend_container_name_required')
        for key in ('ip', 'backendIp', 'agentIp'):
            try:
                ip = ipaddress.ip_address(slot.get(key))
            except (ValueError, TypeError):
                raise PreparationError('invalid_main_app_ip') from None
            if ip.version != 4 or str(ip) != slot[key] or ip not in networks[color] \
                    or ip in (networks[color].network_address, networks[color].broadcast_address,
                              networks[color].network_address + 1) or ip in seen:
                raise PreparationError('invalid_main_app_ip')
            seen.add(ip)
        allowed = DEPLOY_HOME / 'runtime' / color
        directory, file = Path(slot.get('envDir', '')), Path(slot.get('envFile', ''))
        if str(directory) != slot.get('envDir') or '..' in directory.parts \
                or (directory != allowed and allowed not in directory.parents) \
                or str(file) != slot.get('envFile') or file.parent != directory \
                or file.name not in ('frontend.env', '.env'):
            raise PreparationError('main_runtime_path_required')
    return networks


def validate_limits(value):
    if not isinstance(value, dict) or set(value) != {'frontend', 'backend', 'agent'}:
        raise PreparationError('main_app_resource_limits_required')
    for limits in value.values():
        if not isinstance(limits, dict) or set(limits) != {'cpus', 'memoryMb', 'pids'}:
            raise PreparationError('invalid_main_app_resource_limits')
        if type(limits['cpus']) not in (int, float) or not math.isfinite(limits['cpus']) or not 0 < limits['cpus'] <= 64 \
                or type(limits['memoryMb']) is not int or not 64 <= limits['memoryMb'] <= 262144 \
                or type(limits['pids']) is not int or not 64 <= limits['pids'] <= 16384:
            raise PreparationError('invalid_main_app_resource_limits')
    return ('LIMITS_JSON=' + json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


def run(arguments):
    try:
        process = subprocess.run(arguments, check=True, capture_output=True, text=True, timeout=30)
        return process.stdout
    except (OSError, subprocess.SubprocessError):
        raise PreparationError('network_inventory_command_failed') from None


def inspect_networks():
    identities = run(['docker', 'network', 'ls', '--no-trunc', '--format', '{{.ID}}']).splitlines()
    if not identities or any(not re.fullmatch(r'[a-f0-9]{64}', name) for name in identities):
        raise PreparationError('docker_network_inventory_unavailable')
    try:
        result = json.loads(run(['docker', 'network', 'inspect', *identities]))
    except (json.JSONDecodeError, RecursionError):
        raise PreparationError('docker_network_inventory_unavailable') from None
    if not isinstance(result, list) or len(result) != len(identities) \
            or any(not isinstance(item, dict) or not isinstance(item.get('Name'), str)
                   or not isinstance(item.get('Driver'), str) or not isinstance(item.get('IPAM'), dict)
                   for item in result) \
            or {item.get('Id') for item in result} != set(identities):
        raise PreparationError('docker_network_inventory_unavailable')
    for item in result:
        configs = item['IPAM'].get('Config')
        # Docker's built-in host/none networks do not allocate subnets and may
        # report null (or omit Config). Normalize only those exact identities.
        if configs is None and (item['Name'], item['Driver']) in (('host', 'host'), ('none', 'null')):
            item['IPAM']['Config'] = []
        elif not isinstance(configs, list) or any(not isinstance(config, dict) for config in configs):
            raise PreparationError('docker_network_inventory_unavailable')
    return result


def inspect_routes():
    try:
        result = json.loads(run(['ip', '-j', '-4', 'route', 'show', 'table', 'all']))
    except (json.JSONDecodeError, RecursionError):
        raise PreparationError('host_route_inventory_unavailable') from None
    if not isinstance(result, list) or not result or any(not isinstance(item, dict) for item in result):
        raise PreparationError('host_route_inventory_unavailable')
    return result


def validate_inventory(wanted, networks, routes):
    existing = {}
    for role, subnet in wanted.items():
        matching = [n for n in networks if n.get('Name') == EXPECTED_NAMES[role]]
        if len(matching) > 1:
            raise PreparationError('duplicate_main_network_name')
        owned = matching[0] if matching else None
        if owned:
            configs = owned.get('IPAM', {}).get('Config', [])
            if owned.get('Driver') != 'bridge' or owned.get('Internal') is not False \
                    or (owned.get('Labels') or {}).get(ENVIRONMENT_LABEL) != 'main' \
                    or len(configs) != 1 or configs[0].get('Subnet') != str(subnet):
                raise PreparationError('existing_main_network_is_unowned_or_different')
            if any(not c.get('Name', '').startswith('ieum-main-')
                   for c in (owned.get('Containers') or {}).values()):
                raise PreparationError('existing_main_network_has_foreign_containers')
            existing[role] = owned
        for network in networks:
            if network is owned:
                continue
            for config in network.get('IPAM', {}).get('Config', []):
                if not config.get('Subnet'):
                    continue
                try:
                    other = ipaddress.ip_network(config['Subnet'], strict=False)
                except ValueError:
                    raise PreparationError('invalid_existing_docker_subnet') from None
                if other.version == 4 and subnet.overlaps(other):
                    raise PreparationError('main_subnet_overlaps_existing_docker_network')
        own_interface = None
        if owned:
            own_interface = (owned.get('Options') or {}).get('com.docker.network.bridge.name')
            if not own_interface:
                identity = owned.get('Id', '')
                if not re.fullmatch(r'[a-f0-9]{64}', identity):
                    raise PreparationError('owned_main_bridge_identity_unavailable')
                own_interface = 'br-' + identity[:12]
        for route in routes:
            dst = route.get('dst', 'default')
            if dst == 'default':
                continue
            try:
                routed = ipaddress.ip_network(dst, strict=False)
            except (ValueError, TypeError):
                raise PreparationError('invalid_host_route') from None
            if routed.version != 4 or routed.prefixlen == 0 or not subnet.overlaps(routed):
                continue
            if owned and route.get('dev') == own_interface and routed.subnet_of(subnet):
                continue
            raise PreparationError('main_subnet_overlaps_host_lan_or_vpn_route')
    return existing


def directories():
    return (DEPLOY_HOME, DEPLOY_HOME / 'control', DEPLOY_HOME / 'runtime',
            DEPLOY_HOME / 'runtime/blue', DEPLOY_HOME / 'runtime/green', INFRA_HOME)


def validate_directory(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise PreparationError('private_main_directory_required')


def read_private_file(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() \
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
            raise PreparationError('private_owned_main_file_required')
        data = source.read(MAX_INPUT_BYTES + 1)
        if len(data) > MAX_INPUT_BYTES:
            raise PreparationError('private_main_file_too_large')
        return data


def validate_paths(resource_bytes):
    planned = set(directories())
    for path in directories():
        if os.path.lexists(path):
            validate_directory(path)
        elif path.parent not in planned:
            info = path.parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
                raise PreparationError('trusted_existing_deploy_parent_required')
    lock = DEPLOY_HOME / 'deploy.lock'
    if os.path.lexists(lock):
        read_private_file(lock)
    target = DEPLOY_HOME / 'control/resource-limits.env'
    if os.path.lexists(target) and read_private_file(target) != resource_bytes:
        raise PreparationError('main_resource_limits_already_pinned_differently')


def write_new_private(path, content):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


def apply(wanted, resource_bytes):
    # Validation precedes all mutations and is repeated after acquiring the shared MAIN lock.
    validate_paths(resource_bytes)
    validate_inventory(wanted, inspect_networks(), inspect_routes())
    for path in directories():
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            validate_directory(path)
    lock_path = DEPLOY_HOME / 'deploy.lock'
    try:
        write_new_private(lock_path, b'')
    except FileExistsError:
        read_private_file(lock_path)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'r+') as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
            raise PreparationError('private_owned_main_lock_required')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PreparationError('main_deployment_lock_busy') from None
        validate_paths(resource_bytes)
        existing = validate_inventory(wanted, inspect_networks(), inspect_routes())
        for role, subnet in wanted.items():
            if role not in existing:
                run(['docker', 'network', 'create', '--driver', 'bridge', '--subnet', str(subnet),
                     '--label', ENVIRONMENT_LABEL + '=main', '--label', 'ieum.bluegreen.role=' + role,
                     EXPECTED_NAMES[role]])
        validate_inventory(wanted, inspect_networks(), inspect_routes())
        target = DEPLOY_HOME / 'control/resource-limits.env'
        if not os.path.lexists(target):
            write_new_private(target, resource_bytes)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings-file', type=Path, required=True)
    parser.add_argument('--resource-limits', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    try:
        if os.geteuid() == 0 or pwd.getpwuid(os.geteuid()).pw_name != 'kwung':
            raise PreparationError('run_as_kwung_not_root')
        wanted = validate_topology(read_json(args.settings_file))
        limits = read_json(args.resource_limits)
        resource_bytes = validate_limits(limits)
        validate_paths(resource_bytes)
        existing = validate_inventory(wanted, inspect_networks(), inspect_routes())
        if args.apply:
            apply(wanted, resource_bytes)
        print(json.dumps({'operation': 'applied' if args.apply else 'plan',
            'networks': {role: {'name': EXPECTED_NAMES[role], 'subnet': str(subnet),
                                'action': 'reuse' if role in existing else 'create'} for role, subnet in wanted.items()},
            'private_directories': [str(path) for path in directories()],
            'resource_file': str(DEPLOY_HOME / 'control/resource-limits.env'),
            'two_color_app_memory_mb': 2 * sum(value['memoryMb'] for value in limits.values())}, sort_keys=True))
        return 0
    except PreparationError as failure:
        print('MAIN preparation rejected: ' + str(failure) +
              '. No existing resource is altered or removed. An interrupted apply may leave new MAIN resources.', file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        print('MAIN preparation rejected; inspect input, ownership, routes and network conflicts. '
              'No existing resource is altered or removed. An interrupted apply may leave new MAIN resources.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
