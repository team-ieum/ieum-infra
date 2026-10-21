"""Read-only, identity-pinned evidence for the first Legacy JVM time-zone pin.

All Docker operations are injected. The callback must capture output without logging it,
suppress raw diagnostic errors, and enforce the supplied timeout in seconds. This module
never executes Docker, launches an application JVM, or persists the requested zone itself.
"""
import importlib.resources
import json
from pathlib import Path
import re
import shlex
from zoneinfo import TZPATH, available_timezones


ZONES = frozenset(available_timezones()) - {'localtime'}
INSPECT_TIMEOUT = 5
PROBE_TIMEOUT = 10
IDENTITY_FORMAT = ('{"id":{{json .Id}},"image":{{json .Image}},'
                   '"status":{{json .State.Status}},"startedAt":{{json .State.StartedAt}}}')
OPTION_SOURCES = ('JAVA_TOOL_OPTIONS', 'JDK_JAVA_OPTIONS', '_JAVA_OPTIONS', 'JAVA_OPTS')

# Only an allowlisted zone, numeric PID and a fixed status may leave the container.
# jcmd's complete property output and its stderr are never returned to the caller.
# No new `java -XshowSettings` process is used as evidence about the running JVM.
PROPERTY_FILTER = r'''BEGIN { n = split(allowed, names, " "); for (i = 1; i <= n; i++) known[names[i]] = 1; rc = -1 }
/^user[.]timezone=/ { properties++; zone = substr($0, 15); sub(/\r$/, "", zone) }
/^__IEUM_JCMD_STATUS__=/ { rc = substr($0, 22) + 0 }
END {
    if (rc == 0 && properties == 1 && (known[zone] || zone ~ /^GMT[+-][0-9][0-9]?(:[0-9][0-9])?$/))
        printf "ieum-zone-v1|actual|%s|%s\n", pid, zone;
    else if (rc != 0)
        print "ieum-zone-v1|unavailable|attach_failed";
    else
        print "ieum-zone-v1|unavailable|missing_or_unknown_property";
}
'''

PROBE_SCRIPT = r'''set -u
if ! command -v jcmd >/dev/null 2>&1 || ! command -v timeout >/dev/null 2>&1 || ! command -v awk >/dev/null 2>&1; then
    printf 'ieum-zone-v1|unavailable|missing_tools\n'
    exit 0
fi
count=0
pid=
for path in /proc/[0-9]*/comm; do
    comm=
    if IFS= read -r comm < "$path" 2>/dev/null && [ "$comm" = java ]; then
        candidate=${path#/proc/}
        candidate=${candidate%/comm}
        case "$candidate" in ''|*[!0-9]*) continue ;; esac
        count=$((count + 1))
        pid=$candidate
    fi
done
if [ "$count" -ne 1 ]; then
    printf 'ieum-zone-v1|unavailable|ambiguous_jvm\n'
    exit 0
fi
started=$(awk '{print $22}' "/proc/$pid/stat" 2>/dev/null)
case "$started" in ''|*[!0-9]*)
    printf 'ieum-zone-v1|unavailable|ambiguous_jvm\n'
    exit 0 ;;
esac
{
    timeout 5s jcmd "$pid" VM.system_properties 2>/dev/null
    rc=$?
    finished=$(awk '{print $22}' "/proc/$pid/stat" 2>/dev/null)
    if [ "$started" != "$finished" ]; then rc=1; fi
    printf '\n__IEUM_JCMD_STATUS__=%s\n' "$rc"
} | awk -v allowed="$1" -v pid="$pid" '
''' + PROPERTY_FILTER + "'\n"


def _zone(value):
    if not isinstance(value, str) or value not in ZONES or len(value) > 128:
        raise RuntimeError('Legacy time-zone validation requires a reviewed IANA zone identifier')
    return value


def _zone_bytes(zone):
    for directory in TZPATH:
        try:
            path = Path(directory).joinpath(*zone.split('/'))
            data = path.read_bytes()
            if len(data) <= 1024 * 1024:
                return data
        except OSError:
            continue
    try:
        resource = importlib.resources.files('tzdata.zoneinfo').joinpath(*zone.split('/'))
        data = resource.read_bytes()
        return data if len(data) <= 1024 * 1024 else None
    except (ImportError, OSError, TypeError):
        return None


def _equivalent(left, right):
    if left == right:
        return True
    # Complete local TZif definitions, rather than today's UTC offset, identify aliases.
    left_bytes, right_bytes = _zone_bytes(left), _zone_bytes(right)
    return left_bytes is not None and right_bytes is not None and left_bytes == right_bytes


def _observed_valid(zone):
    if zone in ZONES:
        return True
    match = re.fullmatch(r'GMT([+-])([0-9]{1,2})(?::([0-9]{2}))?', zone)
    return bool(match and int(match[2]) <= 23 and int(match[3] or '0') <= 59)


def _observed_matches(zone, requested):
    if zone in ZONES:
        return _equivalent(zone, requested)
    match = re.fullmatch(r'GMT([+-])([0-9]{1,2})(?::([0-9]{2}))?', zone)
    if match is None or int(match[3] or '0') != 0:
        return False
    hours = int(match[2])
    alias = 'Etc/UTC' if hours == 0 else f"Etc/GMT{'-' if match[1] == '+' else '+'}{hours}"
    return alias in ZONES and _equivalent(alias, requested)


def _identity(snapshot, docker_runner):
    try:
        value = docker_runner('inspect', '--format', IDENTITY_FORMAT, snapshot['containerId'],
                              timeout=INSPECT_TIMEOUT)
        if not isinstance(value, str) or len(value) > 4096:
            raise ValueError()
        identity = json.loads(value)
        if identity['id'] != snapshot['containerId'] or identity['image'] != snapshot['image'] or \
                identity['status'] != 'running' or not isinstance(identity.get('startedAt'), str):
            raise ValueError()
        return identity
    except Exception:
        raise RuntimeError('Legacy backend identity changed or could not be verified') from None


def _configuration(environment, requested):
    zones, sources, uncertain = [], [], False
    if 'TZ' in environment:
        value = environment['TZ']
        value = value[1:] if value.startswith(':') else value
        sources.append('TZ')
        if value in ZONES:
            zones.append(value)
        else:
            uncertain = True
    for source in OPTION_SOURCES:
        if source not in environment:
            continue
        try:
            arguments = shlex.split(environment[source])
        except ValueError:
            sources.append(source)
            uncertain = True
            continue
        values = [arg.partition('=')[2] for arg in arguments if arg.startswith('-Duser.timezone=')]
        if '-Duser.timezone' in arguments:
            uncertain = True
        if values or '-Duser.timezone' in arguments:
            sources.append(source)
        for value in values:
            if value in ZONES:
                zones.append(value)
            else:
                uncertain = True
    consistent = bool(zones) and all(_equivalent(zones[0], zone) for zone in zones[1:])
    if uncertain or (zones and not consistent):
        status = 'ambiguous'
    elif not zones:
        status = 'unknown'
    elif _equivalent(zones[0], requested):
        status = 'matches'
    else:
        status = 'mismatch'
    return {'status': status, 'sources': sources, 'zones': sorted(set(zones))}


def _probe(snapshot, docker_runner):
    try:
        output = docker_runner('exec', snapshot['containerId'], 'sh', '-c', PROBE_SCRIPT,
                               'ieum-legacy-zone', ' '.join(sorted(ZONES)), timeout=PROBE_TIMEOUT)
    except Exception:
        return {'status': 'unavailable', 'reason': 'probe_failed'}
    if not isinstance(output, str) or len(output) > 512:
        return {'status': 'unavailable', 'reason': 'invalid_probe_result'}
    fields = output.strip().split('|')
    if len(fields) == 4 and fields[:2] == ['ieum-zone-v1', 'actual'] and \
            re.fullmatch(r'[1-9][0-9]*', fields[2]) and _observed_valid(fields[3]):
        return {'status': 'actual', 'pid': int(fields[2]), 'zone': fields[3]}
    reasons = {'missing_tools', 'ambiguous_jvm', 'attach_failed', 'missing_or_unknown_property'}
    if len(fields) == 3 and fields[:2] == ['ieum-zone-v1', 'unavailable'] and fields[2] in reasons:
        return {'status': 'unavailable', 'reason': fields[2]}
    return {'status': 'unavailable', 'reason': 'invalid_probe_result'}


def validate_legacy_zone(snapshot, requested_zone, confirmation_id, docker_runner):
    """Return sanitized evidence, or reject before the immutable zone is written.

    Only the running JVM property is observational proof. Configuration alone can be
    overridden or ignored, so a matching full container ID is still required when the
    probe is unavailable. A known JVM/configuration mismatch cannot be confirmed away.
    """
    requested = _zone(requested_zone)
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get('containerId'), str) or \
            not re.fullmatch(r'[a-f0-9]{64}', snapshot['containerId']) or \
            not isinstance(snapshot.get('image'), str) or not snapshot['image'] or \
            not isinstance(snapshot.get('startedAt'), str) or not snapshot['startedAt'] or \
            not isinstance(snapshot.get('runtimeEnvironment'), dict) or \
            any(not isinstance(k, str) or not isinstance(v, str) for k, v in snapshot['runtimeEnvironment'].items()):
        raise RuntimeError('Legacy backend evidence requires a pinned container ID, image, start time and runtime environment')
    if confirmation_id is not None and confirmation_id != snapshot['containerId']:
        raise RuntimeError('Legacy time-zone confirmation must match the exact captured 64-character container ID')
    before = _identity(snapshot, docker_runner)
    if before['startedAt'] != snapshot['startedAt']:
        raise RuntimeError('Legacy backend restarted after the captured source snapshot')
    observed = _probe(snapshot, docker_runner)
    after = _identity(snapshot, docker_runner)
    if after != before:
        raise RuntimeError('Legacy backend restarted or changed during time-zone observation')
    configured = _configuration(snapshot['runtimeEnvironment'], requested)
    proof = {'containerId': snapshot['containerId'], 'image': snapshot['image'],
             'startedAt': before['startedAt'], 'configured': configured}
    if observed['status'] == 'actual':
        if not _observed_matches(observed['zone'], requested):
            raise RuntimeError('Legacy running JVM time zone differs from the requested backend time zone')
        proof.update(method='jcmd-VM.system_properties', pid=observed['pid'], observedZone=observed['zone'])
        return {'origin': 'running-jvm', 'effectiveZone': observed['zone'], 'proof': proof}
    if configured['status'] == 'mismatch':
        raise RuntimeError('Legacy time-zone configuration differs from the requested backend time zone')
    if confirmation_id != snapshot['containerId']:
        raise RuntimeError('Running Legacy JVM time zone is unavailable; confirm the exact captured container ID after operator review')
    proof.update(method='explicit-container-confirmation', probeUnavailableReason=observed['reason'],
                 confirmationId=confirmation_id)
    return {'origin': 'operator-confirmed', 'effectiveZone': requested, 'proof': proof}
