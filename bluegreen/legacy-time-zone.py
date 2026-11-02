"""Read-only, identity-pinned evidence for the first Legacy JVM time-zone pin.

All Docker operations are injected. The callback must capture output without logging it,
suppress raw diagnostic errors, and enforce the supplied timeout in seconds. This module
never executes Docker, launches an application JVM, or persists the requested zone itself.
"""
import importlib.resources
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
from zoneinfo import TZPATH, available_timezones


ZONES = frozenset(available_timezones()) - {'localtime'}
INSPECT_TIMEOUT = 5
PROBE_TIMEOUT = 10
PROBE_ATTEMPTS = 2
MIN_PROBE_TIMEOUT = 6
MAX_PROBE_TIMEOUT = 60
MAX_PROBE_ATTEMPTS = 3
IDENTITY_FORMAT = ('{"id":{{json .Id}},"image":{{json .Image}},'
                   '"status":{{json .State.Status}},"startedAt":{{json .State.StartedAt}}}')
OPTION_SOURCES = ('JAVA_TOOL_OPTIONS', 'JDK_JAVA_OPTIONS', '_JAVA_OPTIONS', 'JAVA_OPTS')


class LegacyProbeError(RuntimeError):
    """Fixed diagnostics only: raw Docker errors and output must never escape."""

    CODES = frozenset({'probe_timeout', 'probe_transport_failure', 'probe_command_failed',
                       'probe_invalid_result', 'probe_incomplete_or_changed_process'})

    def __init__(self, code, attempts, timeout_seconds):
        self.code = code if code in self.CODES else 'probe_invalid_result'
        self.attempts = attempts
        self.timeoutSeconds = timeout_seconds
        super().__init__('Legacy Java process changed or returned incomplete time-zone evidence '
                         f'[{self.code}; attempts={attempts}; timeoutSeconds={timeout_seconds}]')


class LegacyConfirmationRequired(RuntimeError):
    """A review challenge over sanitized evidence, never raw environment values."""

    code = 'legacy_zone_confirmation_required'

    def __init__(self, summary):
        self.summary = summary
        self.evidence = summary
        self.fingerprint = hashlib.sha256(json.dumps(
            summary, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()
        super().__init__('Running Legacy JVM time zone is unavailable; review the evidence and confirm '
                         'the exact captured container ID with its evidence SHA-256')

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

# NUL-separated /proc data is reduced inside the container. `od` supplies bounded
# byte values because some awk implementations do not support a NUL record separator.
# Only complete option
# tokens are considered: a credential value containing "-Duser.timezone=" is not
# a VM option. Unsupported quoting and non-allowlisted values become fixed flags.
INPUT_FILTER = r'''BEGIN {
    n = split(allowed, names, " ");
    for (i = 1; i <= n; i++) known[names[i]] = 1;
}
function emit(source, value,    zone) {
    zone = value;
    if (source == "TZ" && substr(zone, 1, 1) == ":") zone = substr(zone, 2);
    if (known[zone] || custom(zone)) print "ieum-zone-v2|input|" source "|zone|" zone;
    else print "ieum-zone-v2|input|" source "|unsupported";
}
function custom(zone,    parts, count) {
    if (zone !~ /^GMT[+-][0-9][0-9]?(:[0-9][0-9])?$/) return 0;
    count = split(substr(zone, 5), parts, ":");
    return parts[1] + 0 <= 23 && (count == 1 || parts[2] + 0 <= 59);
}
function option(source, token) {
    if (substr(token, 1, 16) == "-Duser.timezone=") {
        if (++options[source] <= 32) emit(source, substr(token, 17));
        else if (options[source] == 33) print "ieum-zone-v2|input|" source "|unsupported";
    } else if (token == "-Duser.timezone") print "ieum-zone-v2|input|" source "|unsupported";
}
function optionline(source, value,    i, ch, quote, token, escaped) {
    quote = ""; token = ""; escaped = 0;
    if (value ~ /[\r\n]/) {
        print "ieum-zone-v2|input|" source "|unsupported"; return;
    }
    for (i = 1; i <= length(value); i++) {
        ch = substr(value, i, 1);
        if (escaped) {
            token = token ch; escaped = 0;
        } else if (ch == "\\") {
            escaped = 1;
        } else if (quote != "") {
            if (ch == quote) quote = ""; else token = token ch;
        } else if (ch == "\047" || ch == "\042") {
            quote = ch;
        } else if (ch ~ /[ \t]/) {
            option(source, token); token = "";
        } else token = token ch;
    }
    if (quote != "" || escaped) print "ieum-zone-v2|input|" source "|unsupported";
    else option(source, token);
}
function record(raw,    separator, name, value) {
    records++;
    if (mode == "environment") {
    separator = index(raw, "=");
    if (separator == 0) return;
    name = substr(raw, 1, separator - 1); value = substr(raw, separator + 1);
    if (name == "TZ") {
        if (++seen[name] > 1) print "ieum-zone-v2|input|TZ|unsupported";
        else emit(name, value);
    } else if (name == "JAVA_TOOL_OPTIONS" || name == "JDK_JAVA_OPTIONS" || name == "_JAVA_OPTIONS") {
        if (++seen[name] > 1) print "ieum-zone-v2|input|" name "|unsupported";
        else optionline(name, value);
    }
    } else if (mode == "cmdline") {
    # After the launch target, arguments belong to the application. A timezone
    # shaped application argument must never become VM startup evidence.
    if (records == 1 || target) return;
    if (skip_operand) { skip_operand = 0; return; }
    if (raw == "-jar" || raw == "-m" || raw == "--module" || raw == "--") { target = 1; return; }
    if (raw == "-cp" || raw == "-classpath" || raw == "--class-path" ||
        raw == "-p" || raw == "--module-path" || raw == "--upgrade-module-path" ||
        raw == "--add-modules" || raw == "--limit-modules" ||
        raw == "--add-exports" || raw == "--add-opens" || raw == "--add-reads" ||
        raw == "--patch-module" || raw == "--enable-native-access" || raw == "--source") { skip_operand = 1; return; }
    if (substr(raw, 1, 1) == "@") { print "ieum-zone-v2|input|cmdline|unsupported"; return; }
    if (substr(raw, 1, 1) != "-") { target = 1; return; }
    option("cmdline", raw);
    }
}
/^__IEUM_PROC_READ_EXIT__=/ {
    read_markers++;
    if ($0 != "__IEUM_PROC_READ_EXIT__=0" || read_markers != 1) {
        print "ieum-zone-v2|invalid|proc_read_failed"; invalid = 1;
    }
    next;
}
{
    if (read_markers) { print "ieum-zone-v2|invalid|data_after_read_status"; invalid = 1; exit; }
    for (byte_index = 1; byte_index <= NF; byte_index++) {
        if ($byte_index !~ /^[0-9]+$/ || $byte_index + 0 > 255) {
            print "ieum-zone-v2|invalid|invalid_byte_stream"; invalid = 1; exit;
        }
        if (++bytes > 131072) { print "ieum-zone-v2|invalid|input_too_large"; invalid = 1; exit; }
        if ($byte_index + 0 == 0) { record(buffer); buffer = ""; }
        else buffer = buffer sprintf("%c", $byte_index + 0);
    }
}
END {
    if (read_markers != 1 && !invalid) { print "ieum-zone-v2|invalid|missing_read_status"; invalid = 1; }
    if (mode == "cmdline" && records == 0 && !invalid) { print "ieum-zone-v2|invalid|empty_cmdline"; invalid = 1; }
    if (buffer != "" && !invalid) { print "ieum-zone-v2|invalid|unterminated_proc_input"; invalid = 1; }
    state = invalid ? "unavailable" : "ok";
    if (mode == "environment") print "ieum-zone-v2|read|environment|" state;
    else print "ieum-zone-v2|read|cmdline|" state;
}
'''

FILE_FILTER = r'''BEGIN {
    n = split(allowed, names, " "); for (i = 1; i <= n; i++) known[names[i]] = 1;
}
{ if (NR == 1) zone = $0; else bad = 1; }
END {
    if (!bad && known[zone]) print "ieum-zone-v2|input|" source "|zone|" zone;
    else print "ieum-zone-v2|input|" source "|unsupported";
}
'''

# Compare with the container's own TZDB, not the controller host's TZDB digest.
# The caller enforces a three-second deadline. Both file sizes and cmp's byte
# count are capped, and iteration is over the finite caller-supplied allowlist.
LOCALTIME_RESOLVER_SCRIPT = r'''set -u
LC_ALL=C
export LC_ALL
file=$2
directory=$3
command -v cmp >/dev/null 2>&1 && command -v wc >/dev/null 2>&1 || exit 1
cmp -s -n 1 /dev/null /dev/null >/dev/null 2>&1 || exit 1
size=$(wc -c < "$file" 2>/dev/null) || exit 1
size=${size##* }
case "$size" in ''|*[!0-9]*) exit 1 ;; esac
[ "$size" -gt 0 ] && [ "$size" -le 1048576 ] || exit 1
signature=$(od -An -tu1 -N4 "$file" 2>/dev/null | awk '{$1=$1; print}')
[ "$signature" = '84 90 105 102' ] || exit 1
count=0
for zone in $1; do
    count=$((count + 1))
    [ "$count" -le 1024 ] || exit 1
    candidate=$directory/$zone
    [ -f "$candidate" ] && [ -r "$candidate" ] || continue
    candidate_size=$(wc -c < "$candidate" 2>/dev/null) || continue
    candidate_size=${candidate_size##* }
    [ "$candidate_size" = "$size" ] || continue
    if cmp -s -n 1048577 "$file" "$candidate" >/dev/null 2>&1; then
        after=$(wc -c < "$file" 2>/dev/null) || exit 1
        candidate_after=$(wc -c < "$candidate" 2>/dev/null) || exit 1
        after=${after##* }
        candidate_after=${candidate_after##* }
        [ "$after" = "$size" ] && [ "$candidate_after" = "$size" ] || exit 1
        printf '%s\n' "$zone"
        exit 0
    fi
done
exit 1
'''

PROBE_SCRIPT = r'''set -u
LC_ALL=C
export LC_ALL
if ! command -v awk >/dev/null 2>&1 || ! command -v head >/dev/null 2>&1 || ! command -v od >/dev/null 2>&1; then
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
started=$(awk '{ sub(/^[0-9]+ [(].*[)] /, ""); print $20 }' "/proc/$pid/stat" 2>/dev/null)
case "$started" in ''|*[!0-9]*)
    printf 'ieum-zone-v1|unavailable|ambiguous_jvm\n'
    exit 0 ;;
esac
printf 'ieum-zone-v2|process|%s|%s\n' "$pid" "$started"
if command -v jcmd >/dev/null 2>&1 && command -v timeout >/dev/null 2>&1; then
{
    timeout 5s jcmd "$pid" VM.system_properties 2>/dev/null
    rc=$?
    finished=$(awk '{ sub(/^[0-9]+ [(].*[)] /, ""); print $20 }' "/proc/$pid/stat" 2>/dev/null)
    if [ "$started" != "$finished" ]; then rc=1; fi
    printf '\n__IEUM_JCMD_STATUS__=%s\n' "$rc"
} | awk -v allowed="$1" -v pid="$pid" '
''' + PROPERTY_FILTER + "'" + r'''
else
    printf 'ieum-zone-v1|unavailable|missing_tools\n'
fi
if [ -r "/proc/$pid/environ" ]; then
    { od -An -v -tu1 -N131073 "/proc/$pid/environ" 2>/dev/null; printf '__IEUM_PROC_READ_EXIT__=%s\n' "$?"; } | awk -v allowed="$1" -v mode=environment '
''' + INPUT_FILTER + "'" + r''' 2>/dev/null
else
    printf 'ieum-zone-v2|read|environment|unavailable\n'
fi
if [ -r "/proc/$pid/cmdline" ]; then
    { od -An -v -tu1 -N131073 "/proc/$pid/cmdline" 2>/dev/null; printf '__IEUM_PROC_READ_EXIT__=%s\n' "$?"; } | awk -v allowed="$1" -v mode=cmdline '
''' + INPUT_FILTER + "'" + r''' 2>/dev/null
else
    printf 'ieum-zone-v2|read|cmdline|unavailable\n'
fi
if [ -r /etc/timezone ]; then
    head -c 4097 /etc/timezone 2>/dev/null | awk -v allowed="$1" -v source=etc-timezone '
''' + FILE_FILTER + "'" + r''' 2>/dev/null
else
    printf 'ieum-zone-v2|input|etc-timezone|missing\n'
fi
link=
if command -v readlink >/dev/null 2>&1; then link=$(readlink /etc/localtime 2>/dev/null) || link=; fi
case "$link" in
    /usr/share/zoneinfo/*) link=${link#/usr/share/zoneinfo/} ;;
    ../usr/share/zoneinfo/*) link=${link#../usr/share/zoneinfo/} ;;
    *) link= ;;
esac
resolution=missing
if [ -n "$link" ] && case " $1 " in *" $link "*) true ;; *) false ;; esac; then
    printf '%s\n' "$link" | awk -v allowed="$1" -v source=etc-localtime '
''' + FILE_FILTER + "'" + r'''
    resolution=symlink
elif [ -e /etc/localtime ] || [ -L /etc/localtime ]; then
    resolution=unresolved
    resolved=
    if [ -f /etc/localtime ] && [ -r /etc/localtime ] && command -v timeout >/dev/null 2>&1; then
        resolved=$(timeout 3s sh -c ''' + shlex.quote(LOCALTIME_RESOLVER_SCRIPT) + r''' ieum-localtime "$1" /etc/localtime /usr/share/zoneinfo 2>/dev/null) || resolved=
    fi
    if [ -n "$resolved" ] && case " $1 " in *" $resolved "*) true ;; *) false ;; esac; then
        printf '%s\n' "$resolved" | awk -v allowed="$1" -v source=etc-localtime '
''' + FILE_FILTER + "'" + r'''
        resolution=resolved
    else
        printf 'ieum-zone-v2|input|etc-localtime|unsupported\n'
    fi
else
    printf 'ieum-zone-v2|input|etc-localtime|missing\n'
fi
printf 'ieum-zone-v2|localtime-resolution|%s\n' "$resolution"
if [ -r /etc/localtime ] && command -v sha256sum >/dev/null 2>&1; then
    head -c 1048577 /etc/localtime 2>/dev/null | sha256sum 2>/dev/null | awk '
        $1 ~ /^[a-f0-9]+$/ && length($1) == 64 { print "ieum-zone-v2|localtime-sha256|" $1; found=1 }
        END { if (!found) print "ieum-zone-v2|localtime-sha256|unavailable" }'
else
    printf 'ieum-zone-v2|localtime-sha256|unavailable\n'
fi
finished=$(awk '{ sub(/^[0-9]+ [(].*[)] /, ""); print $20 }' "/proc/$pid/stat" 2>/dev/null)
count=0
for path in /proc/[0-9]*/comm; do
    comm=
    if IFS= read -r comm < "$path" 2>/dev/null && [ "$comm" = java ]; then
        candidate=${path#/proc/}; candidate=${candidate%/comm}
        count=$((count + 1))
        if [ "$candidate" != "$pid" ]; then count=2; break; fi
    fi
done
if [ "$started" != "$finished" ] || [ "$count" -ne 1 ]; then
    printf 'ieum-zone-v2|invalid|pid_changed\n'
else
    printf 'ieum-zone-v2|complete\n'
fi
'''


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


def _observed_rules_zone(zone):
    if zone in ZONES:
        return zone
    match = re.fullmatch(r'GMT([+-])([0-9]{1,2})(?::([0-9]{2}))?', zone)
    if match is None or int(match[2]) > 23 or int(match[3] or '0') != 0:
        return None
    hours = int(match[2])
    alias = 'Etc/UTC' if hours == 0 else f"Etc/GMT{'-' if match[1] == '+' else '+'}{hours}"
    return alias if alias in ZONES else None


def _observed_matches(zone, requested):
    observed = _observed_rules_zone(zone)
    return observed is not None and _equivalent(observed, requested)


def _observed_equivalent(left, right):
    if left == right:
        return True
    left, right = _observed_rules_zone(left), _observed_rules_zone(right)
    return left is not None and right is not None and _equivalent(left, right)


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
        if _observed_valid(value):
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
            if _observed_valid(value):
                zones.append(value)
            else:
                uncertain = True
    consistent = bool(zones) and all(_observed_equivalent(zones[0], zone) for zone in zones[1:])
    if uncertain or (zones and not consistent):
        status = 'ambiguous'
    elif not zones:
        status = 'unknown'
    elif _observed_matches(zones[0], requested):
        status = 'matches'
    else:
        status = 'mismatch'
    return {'status': status, 'sources': sources, 'zones': sorted(set(zones))}


def _probe_settings(timeout, attempts):
    if isinstance(timeout, bool) or not isinstance(timeout, int) or \
            not MIN_PROBE_TIMEOUT <= timeout <= MAX_PROBE_TIMEOUT:
        raise RuntimeError('Legacy probe timeout must be an integer from 6 through 60 seconds')
    if isinstance(attempts, bool) or not isinstance(attempts, int) or not 1 <= attempts <= MAX_PROBE_ATTEMPTS:
        raise RuntimeError('Legacy probe attempts must be an integer from 1 through 3')


def _probe(snapshot, docker_runner, timeout=PROBE_TIMEOUT, attempts=PROBE_ATTEMPTS):
    """Retry only an explicit timeout or transport exception, never bad evidence.

    A nonzero command result is not proof of a transient transport failure. The
    injected runner may signal a recognized connection failure with ConnectionError;
    no stderr text or arbitrary exception message is inspected or returned here.
    """
    for attempt in range(1, attempts + 1):
        retryable = False
        try:
            output = docker_runner('exec', snapshot['containerId'], 'sh', '-c', PROBE_SCRIPT,
                                   'ieum-legacy-zone', ' '.join(sorted(ZONES)), timeout=timeout)
        except (TimeoutError, subprocess.TimeoutExpired):
            code, retryable = 'probe_timeout', True
        except ConnectionError:
            code, retryable = 'probe_transport_failure', True
        except Exception:
            code = 'probe_command_failed'
        else:
            if not isinstance(output, str) or len(output) > 32768:
                observed = {'status': 'invalid', 'reason': 'invalid_probe_result'}
            elif output.startswith('ieum-zone-v2|'):
                observed = _probe_v2(output)
            else:
                observed = _probe_v1(output)
            if observed['status'] != 'invalid':
                observed['probe'] = {'code': 'probe_ok', 'attempts': attempt, 'timeoutSeconds': timeout}
                return observed
            code = ('probe_incomplete_or_changed_process'
                    if observed['reason'] == 'incomplete_or_changed_process' else 'probe_invalid_result')
        if not retryable or attempt == attempts:
            return {'status': 'invalid', 'reason': code,
                    'probe': {'code': code, 'attempts': attempt, 'timeoutSeconds': timeout}}


def _probe_v1(output):
    fields = output.strip().split('|')
    if len(fields) == 4 and fields[:2] == ['ieum-zone-v1', 'actual'] and \
            re.fullmatch(r'[1-9][0-9]*', fields[2]) and _observed_valid(fields[3]):
        return {'status': 'actual', 'pid': int(fields[2]), 'zone': fields[3]}
    reasons = {'missing_tools', 'ambiguous_jvm', 'attach_failed', 'missing_or_unknown_property'}
    if len(fields) == 3 and fields[:2] == ['ieum-zone-v1', 'unavailable'] and fields[2] in reasons:
        return {'status': 'unavailable', 'reason': fields[2]}
    return {'status': 'invalid', 'reason': 'invalid_probe_result'}


def _probe_v2(output):
    """Decode only the fixed, in-container-sanitized record grammar."""
    inputs, reads, process, observed, digest, resolution = {}, {}, None, None, None, None
    lines = output.rstrip('\n').split('\n')
    if not lines or lines[-1] != 'ieum-zone-v2|complete' or len(lines) > 160:
        return {'status': 'invalid', 'reason': 'incomplete_or_changed_process'}
    for line in lines[:-1]:
        fields = line.split('|')
        if line.startswith('ieum-zone-v1|') and observed is None:
            observed = _probe_v1(line)
            if observed.get('reason') == 'invalid_probe_result':
                return {'status': 'invalid', 'reason': 'invalid_probe_result'}
        elif len(fields) == 4 and fields[:2] == ['ieum-zone-v2', 'process'] and process is None and \
                all(re.fullmatch(r'[1-9][0-9]*', item) for item in fields[2:]):
            process = {'pid': int(fields[2]), 'startTicks': fields[3]}
        elif len(fields) == 4 and fields[:2] == ['ieum-zone-v2', 'read'] and \
                fields[2] in ('environment', 'cmdline') and fields[3] in ('ok', 'unavailable') and fields[2] not in reads:
            reads[fields[2]] = fields[3]
        elif len(fields) in (4, 5) and fields[:2] == ['ieum-zone-v2', 'input'] and \
                fields[2] in ('TZ', 'JAVA_TOOL_OPTIONS', 'JDK_JAVA_OPTIONS', '_JAVA_OPTIONS',
                              'cmdline', 'etc-timezone', 'etc-localtime'):
            if len(fields) == 5 and fields[3] == 'zone' and _observed_valid(fields[4]):
                item = {'status': 'zone', 'zone': fields[4]}
            elif len(fields) == 4 and fields[3] == 'unsupported':
                item = {'status': 'unsupported'}
            elif len(fields) == 4 and fields[3] == 'missing' and fields[2].startswith('etc-'):
                item = {'status': 'missing'}
            else:
                return {'status': 'invalid', 'reason': 'invalid_probe_result'}
            inputs.setdefault(fields[2], []).append(item)
        elif len(fields) == 3 and fields[:2] == ['ieum-zone-v2', 'localtime-sha256'] and digest is None and \
                (fields[2] == 'unavailable' or re.fullmatch(r'[a-f0-9]{64}', fields[2])):
            digest = fields[2]
        elif len(fields) == 3 and fields[:2] == ['ieum-zone-v2', 'localtime-resolution'] and resolution is None and \
                fields[2] in ('symlink', 'resolved', 'missing', 'unresolved'):
            resolution = fields[2]
        else:
            return {'status': 'invalid', 'reason': 'invalid_probe_result'}
    if process is None or observed is None or set(reads) != {'environment', 'cmdline'} or digest is None or resolution is None or \
            any(len(inputs.get(source, [])) != 1 for source in ('etc-timezone', 'etc-localtime')) or \
            (observed['status'] == 'actual' and observed['pid'] != process['pid']):
        return {'status': 'invalid', 'reason': 'incomplete_or_changed_process'}
    expected_localtime_status = {'symlink': 'zone', 'resolved': 'zone', 'missing': 'missing', 'unresolved': 'unsupported'}
    if inputs['etc-localtime'][0]['status'] != expected_localtime_status[resolution]:
        return {'status': 'invalid', 'reason': 'invalid_probe_result'}
    observed.update(process=process, inputs=inputs, reads=reads, localtimeSha256=digest,
                    localtimeResolution=resolution)
    return observed


def _startup_inputs(observed, requested):
    """Conservative startup evidence, never proof of cached/runtime JVM state.

    `user.timezone` overrides platform discovery. JVM environment option ordering
    and launcher argument files are deliberately not guessed: conflicting possible
    property values reject confirmation when both process inputs were read. With
    no recognized property, TZ precedes the platform files. Unavailable environment
    or argv may contain an overriding property, so a single lower-priority zone
    cannot establish a mismatch. Invalid or partially read data never reaches this
    function. Contradictory platform files and unresolved localtime require a fully
    read explicit TZ/property override; incomplete process inputs cannot confirm
    around either condition. JDK patches may consult different platform files.
    """
    inputs = observed['inputs']
    properties = [item for source in ('JAVA_TOOL_OPTIONS', 'JDK_JAVA_OPTIONS', '_JAVA_OPTIONS', 'cmdline')
                  for item in inputs.get(source, [])]
    if any(item['status'] == 'zone' for item in properties):
        selected, basis = properties, 'jvm-startup-properties'
    elif any(item['status'] == 'zone' for item in inputs.get('TZ', [])):
        selected, basis = inputs['TZ'], 'process-TZ'
    else:
        selected = inputs['etc-timezone'] + inputs['etc-localtime']
        basis = 'platform-files-jdk-version-unspecified'
    zones = sorted({item['zone'] for item in selected if item['status'] == 'zone'})
    # A digest match adds support without treating different TZDB bytes as proof
    # of different semantics. Zone names still require full TZif alias matching.
    requested_bytes = _zone_bytes(requested)
    digest_match = bool(requested_bytes is not None and observed['localtimeSha256'] != 'unavailable' and
                        hashlib.sha256(requested_bytes).hexdigest() == observed['localtimeSha256'])
    known_mismatch = any(not _observed_matches(value, requested) for value in zones)
    complete_process_inputs = all(value == 'ok' for value in observed['reads'].values())
    platform_zones = [item['zone'] for source in ('etc-timezone', 'etc-localtime')
                      for item in inputs.get(source, []) if item['status'] == 'zone']
    platform_conflict = bool(platform_zones) and any(
        not _observed_equivalent(platform_zones[0], value) for value in platform_zones[1:])
    explicit_override = complete_process_inputs and basis in ('jvm-startup-properties', 'process-TZ')
    uncertain = (any(item['status'] == 'unsupported' for values in inputs.values() for item in values) or
                 not complete_process_inputs)
    if platform_conflict and not explicit_override:
        status = 'mismatch'
    elif observed['localtimeResolution'] == 'unresolved' and not explicit_override:
        status = 'unresolved'
    elif known_mismatch and complete_process_inputs:
        status = 'mismatch'
    elif not complete_process_inputs:
        status = 'ambiguous'
    elif zones or (basis.startswith('platform-files') and digest_match):
        status = 'ambiguous' if uncertain else 'matches'
    else:
        status = 'unknown'
    return {'status': status, 'basis': basis, 'zones': zones,
            'inputs': inputs, 'reads': observed['reads'], 'localtimeSha256': observed['localtimeSha256'],
            'processInputsComplete': complete_process_inputs,
            'localtimeResolution': observed['localtimeResolution'], 'platformInputsConflict': platform_conflict,
            'requestedTzifDigestMatches': digest_match,
            'limitation': 'startup inputs do not prove cached JVM state or application TimeZone.setDefault overrides'}


def _confirmation_summary(snapshot, requested, configured, observed, startup):
    summary = {'schemaVersion': 1, 'expectedZone': requested, 'containerId': snapshot['containerId'],
               'imageIdentitySha256': hashlib.sha256(snapshot['image'].encode()).hexdigest(),
               'containerStartIdentitySha256': hashlib.sha256(snapshot['startedAt'].encode()).hexdigest(),
               'configured': configured, 'probeUnavailableReason': observed['reason']}
    if startup is not None:
        summary['pid'] = observed['process']['pid']
        summary['pidStartTicks'] = observed['process']['startTicks']
        summary['startupInputs'] = {key: startup[key] for key in (
            'status', 'basis', 'zones', 'processInputsComplete', 'reads', 'localtimeSha256',
            'requestedTzifDigestMatches', 'localtimeResolution', 'platformInputsConflict')}
        summary['startupInputs']['sourceKinds'] = {
            source: sorted({item['status'] for item in values})
            for source, values in startup['inputs'].items()}
        summary['startupInputs']['observedZones'] = {
            source: sorted({item['zone'] for item in values if item['status'] == 'zone'})
            for source, values in startup['inputs'].items()}
    return summary


def _observed_summary(snapshot, requested, configured, observed):
    summary = {'schemaVersion': 1, 'expectedZone': requested, 'containerId': snapshot['containerId'],
               'imageIdentitySha256': hashlib.sha256(snapshot['image'].encode()).hexdigest(),
               'containerStartIdentitySha256': hashlib.sha256(snapshot['startedAt'].encode()).hexdigest(),
               'configured': configured, 'observedZone': observed['zone'], 'pid': observed['pid']}
    if 'process' in observed:
        summary['pidStartTicks'] = observed['process']['startTicks']
    return summary


def _assess_legacy_zone(snapshot, requested_zone, docker_runner, *,
                        probe_timeout=PROBE_TIMEOUT, probe_attempts=PROBE_ATTEMPTS,
                        confirmation_id=None):
    """Capture once and classify evidence without granting operator approval.

    Both the read-only inspection command and preparation use this path. Invalid
    transport, process, or identity evidence still raises instead of becoming a
    displayable or attestable assessment.
    """
    requested = _zone(requested_zone)
    _probe_settings(probe_timeout, probe_attempts)
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
    observed = _probe(snapshot, docker_runner, probe_timeout, probe_attempts)
    after = _identity(snapshot, docker_runner)
    if after != before:
        raise RuntimeError('Legacy backend restarted or changed during time-zone observation')
    if observed['status'] == 'invalid':
        raise LegacyProbeError(observed['probe']['code'], observed['probe']['attempts'],
                               observed['probe']['timeoutSeconds']) from None
    configured = _configuration(snapshot['runtimeEnvironment'], requested)
    proof = {'containerId': snapshot['containerId'], 'image': snapshot['image'],
             'startedAt': before['startedAt'], 'configured': configured, 'probe': observed['probe']}
    if observed['status'] == 'actual':
        summary = _observed_summary(snapshot, requested, configured, observed)
        if not _observed_matches(observed['zone'], requested):
            return {'status': 'blocked', 'reason': 'observed_jvm_mismatch', 'evidence': summary,
                    'error': 'Legacy running JVM time zone differs from the requested backend time zone'}
        proof.update(method='jcmd-VM.system_properties', pid=observed['pid'], observedZone=observed['zone'],
                     limitation='user.timezone property does not prove a later application TimeZone.setDefault override')
        if 'process' in observed:
            proof['pidStartTicks'] = observed['process']['startTicks']
        return {'status': 'running-jvm', 'reason': 'observed_jvm_match', 'evidence': summary,
                'result': {'origin': 'running-jvm', 'effectiveZone': observed['zone'], 'proof': proof}}
    startup = _startup_inputs(observed, requested) if 'inputs' in observed else None
    summary = _confirmation_summary(snapshot, requested, configured, observed, startup)
    if startup is not None:
        if startup['status'] == 'unresolved':
            return {'status': 'blocked', 'reason': 'localtime_unresolved', 'evidence': summary,
                    'error': 'Legacy localtime could not be resolved against the container zoneinfo; explicit process time-zone evidence is required'}
        if startup['status'] == 'mismatch':
            explicit_override = startup['processInputsComplete'] and startup['basis'] in (
                'jvm-startup-properties', 'process-TZ')
            reason = 'platform_conflict' if startup['platformInputsConflict'] and not explicit_override else 'startup_mismatch'
            return {'status': 'blocked', 'reason': reason, 'evidence': summary,
                    'error': 'Legacy process or platform time-zone startup evidence differs from the requested backend time zone'}
        if not startup['processInputsComplete'] and \
                any(not _observed_matches(value, requested) for value in configured['zones']):
            return {'status': 'blocked', 'reason': 'configuration_mismatch', 'evidence': summary,
                    'error': 'Legacy time-zone configuration differs from the requested backend time zone'}
        proof.update(startupInputs=startup, pid=observed['process']['pid'],
                     pidStartTicks=observed['process']['startTicks'])
        # Fully read process inputs supersede Config.Env hints. When one is not
        # readable, known Config.Env mismatches have already rejected confirmation.
        # JAVA_OPTS is not consumed directly by the JVM and may be launcher-specific.
        configured = startup
    if configured['status'] == 'mismatch':
        return {'status': 'blocked', 'reason': 'configuration_mismatch', 'evidence': summary,
                'error': 'Legacy time-zone configuration differs from the requested backend time zone'}
    return {'status': 'confirmation-required', 'reason': 'operator_confirmation_required',
            'evidence': summary, 'proof': proof, 'requestedZone': requested,
            'probeUnavailableReason': observed['reason']}


def inspect_legacy_zone(snapshot, requested_zone, docker_runner, *,
                        probe_timeout=PROBE_TIMEOUT, probe_attempts=PROBE_ATTEMPTS):
    """Return sanitized diagnostic evidence without approving or persisting it.

    Only a confirmation-required result supplies a fingerprint that can be used
    with the exact container ID during prepare. A blocked result's fingerprint is
    diagnostic only; it never turns a refusal into operator approval.
    """
    assessment = _assess_legacy_zone(snapshot, requested_zone, docker_runner,
        probe_timeout=probe_timeout, probe_attempts=probe_attempts)
    summary = assessment['evidence']
    return {'status': assessment['status'], 'reason': assessment['reason'], 'evidence': summary,
            'evidenceSha256': hashlib.sha256(json.dumps(
                summary, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()}


def validate_legacy_zone(snapshot, requested_zone, confirmation_id, docker_runner, *,
                         probe_timeout=PROBE_TIMEOUT, probe_attempts=PROBE_ATTEMPTS,
                         confirmation_evidence=None):
    """Return a verified preparation proof or reject before immutable values.

    Read-only inspection and prepare share one capture/classification path, but
    inspecting never approves evidence. No-attach preparation still needs both
    the exact captured container ID and the unchanged sanitized evidence SHA-256.
    """
    if confirmation_evidence is not None and (not isinstance(confirmation_evidence, str) or
                                              not re.fullmatch(r'[a-f0-9]{64}', confirmation_evidence)):
        raise RuntimeError('Legacy time-zone confirmation evidence must be a 64-character SHA-256')
    assessment = _assess_legacy_zone(snapshot, requested_zone, docker_runner,
        probe_timeout=probe_timeout, probe_attempts=probe_attempts, confirmation_id=confirmation_id)
    if assessment['status'] == 'blocked':
        raise RuntimeError(assessment['error'])
    if assessment['status'] == 'running-jvm':
        return assessment['result']
    challenge = LegacyConfirmationRequired(assessment['evidence'])
    if confirmation_id != snapshot['containerId'] or confirmation_evidence != challenge.fingerprint:
        raise challenge
    proof = assessment['proof']
    proof.update(method='explicit-container-confirmation', probeUnavailableReason=assessment['probeUnavailableReason'],
                 confirmationId=confirmation_id, confirmationEvidence=challenge.fingerprint,
                 confirmationSummary=challenge.summary)
    return {'origin': 'operator-confirmed', 'effectiveZone': assessment['requestedZone'], 'proof': proof}
