import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


spec = importlib.util.spec_from_file_location('legacy_time_zone', Path(__file__).with_name('legacy-time-zone.py'))
zone = importlib.util.module_from_spec(spec)
spec.loader.exec_module(zone)
CID = 'a' * 64
IMAGE = 'sha256:' + 'b' * 64
SECRET = 'credential-that-must-never-leave-diagnostics'


def snapshot(environment=None):
    return {'containerId': CID, 'image': IMAGE, 'startedAt': '2026-10-20T00:00:00Z',
            'runtimeEnvironment': environment or {}}


class DockerProbe:
    def __init__(self, probe='ieum-zone-v1|actual|17|UTC', identities=None):
        self.probe = probe
        self.identities = identities
        self.calls = []

    def __call__(self, *args, timeout):
        self.calls.append((args, timeout))
        if args[0] == 'inspect':
            if self.identities is not None:
                value = self.identities.pop(0)
                if isinstance(value, Exception):
                    raise value
                return json.dumps(value)
            return json.dumps(identity())
        if args[0] != 'exec':
            raise AssertionError('Only identity inspection and a pinned read-only probe are allowed')
        if isinstance(self.probe, Exception):
            raise self.probe
        return self.probe


def identity(**changes):
    return dict(id=CID, image=IMAGE, status='running', startedAt='2026-10-20T00:00:00Z', **changes)


def startup_probe(*records, actual='ieum-zone-v1|unavailable|missing_tools', pid=17, ticks=123):
    return '\n'.join([f'ieum-zone-v2|process|{pid}|{ticks}', actual,
                      'ieum-zone-v2|read|environment|ok', 'ieum-zone-v2|read|cmdline|ok',
                      *records,
                      *([] if any('|etc-timezone|' in item for item in records)
                        else ['ieum-zone-v2|input|etc-timezone|missing']),
                      *([] if any('|etc-localtime|' in item for item in records)
                        else ['ieum-zone-v2|input|etc-localtime|missing']),
                      *([] if any('|localtime-sha256|' in item for item in records)
                        else ['ieum-zone-v2|localtime-sha256|unavailable']),
                      'ieum-zone-v2|complete'])


def filter_proc(payload, mode, status='__IEUM_PROC_READ_EXIT__=0\n'):
    encoded = subprocess.run(['od', '-An', '-v', '-tu1'], input=payload, check=True, capture_output=True)
    return subprocess.run(['awk', '-v', 'allowed=UTC Etc/UTC Asia/Seoul', '-v', 'mode=' + mode, zone.INPUT_FILTER],
                          input=encoded.stdout + status.encode(), check=True, capture_output=True)


class LegacyTimeZoneTest(unittest.TestCase):
    def test_actual_jvm_matches_without_confirmation_and_is_pinned_before_and_after(self):
        docker = DockerProbe()
        result = zone.validate_legacy_zone(snapshot({'PASSWORD': SECRET}), 'UTC', None, docker)
        self.assertEqual(result['origin'], 'running-jvm')
        self.assertEqual(result['effectiveZone'], 'UTC')
        self.assertEqual(result['proof']['pid'], 17)
        self.assertEqual(result['proof']['containerId'], CID)
        self.assertEqual(result['proof']['image'], IMAGE)
        self.assertEqual([call[0][0] for call in docker.calls], ['inspect', 'exec', 'inspect'])
        for args, timeout in docker.calls:
            self.assertIn(CID, args)
            self.assertIn(timeout, (5, 10))
            self.assertNotIn('ieum-backend', args)
        self.assertNotIn(SECRET, json.dumps(result))

    def test_actual_mismatch_cannot_be_overridden_by_confirmation(self):
        for confirmation in (None, CID):
            with self.subTest(confirmation=confirmation), self.assertRaisesRegex(RuntimeError, 'running JVM time zone differs'):
                zone.validate_legacy_zone(snapshot({'TZ': 'Asia/Seoul'}), 'Asia/Seoul', confirmation, DockerProbe())

    def test_actual_jvm_wins_over_conflicting_environment_evidence(self):
        result = zone.validate_legacy_zone(snapshot({'TZ': 'Asia/Seoul'}), 'UTC', None, DockerProbe())
        self.assertEqual(result['origin'], 'running-jvm')
        self.assertEqual(result['proof']['configured']['status'], 'mismatch')

    def test_full_rule_alias_is_accepted_but_same_current_offset_is_not(self):
        result = zone.validate_legacy_zone(snapshot(), 'Etc/UTC', None, DockerProbe())
        self.assertEqual(result['effectiveZone'], 'UTC')
        with self.assertRaisesRegex(RuntimeError, 'running JVM time zone differs'):
            zone.validate_legacy_zone(snapshot(), 'Etc/GMT-9', CID, DockerProbe('ieum-zone-v1|actual|17|Asia/Seoul'))

    def test_known_fixed_jvm_offset_cannot_be_confirmed_as_region_rules(self):
        with self.assertRaisesRegex(RuntimeError, 'running JVM time zone differs'):
            zone.validate_legacy_zone(snapshot(), 'Asia/Seoul', CID, DockerProbe('ieum-zone-v1|actual|17|GMT+09:00'))
        result = zone.validate_legacy_zone(snapshot(), 'UTC', None, DockerProbe('ieum-zone-v1|actual|17|GMT+00:00'))
        self.assertEqual(result['origin'], 'running-jvm')

    def test_missing_jcmd_and_no_tz_never_implies_image_default_utc(self):
        docker = DockerProbe('ieum-zone-v1|unavailable|missing_tools')
        with self.assertRaisesRegex(RuntimeError, 'exact captured container ID'):
            zone.validate_legacy_zone(snapshot(), 'Asia/Seoul', None, docker)
        result = zone.validate_legacy_zone(snapshot(), 'Asia/Seoul', CID, docker)
        self.assertEqual(result['origin'], 'operator-confirmed')
        self.assertEqual(result['effectiveZone'], 'Asia/Seoul')
        self.assertEqual(result['proof']['configured']['status'], 'unknown')
        self.assertEqual(result['proof']['confirmationId'], CID)
        self.assertNotIn('pid', result['proof'])

    def test_matching_configuration_still_requires_operator_confirmation(self):
        for environment in ({'TZ': 'UTC'}, {'TZ': ':UTC'}, {'JAVA_TOOL_OPTIONS': '-Xmx512m -Duser.timezone="UTC"'},
                            {'JAVA_OPTS': '-Duser.timezone=UTC'}, {'_JAVA_OPTIONS': '-Duser.timezone=Etc/UTC'},
                            {'JDK_JAVA_OPTIONS': '-Duser.timezone=UTC'}):
            with self.subTest(source=list(environment)):
                docker = DockerProbe('ieum-zone-v1|unavailable|missing_tools')
                with self.assertRaisesRegex(RuntimeError, 'exact captured container ID'):
                    zone.validate_legacy_zone(snapshot(environment), 'UTC', None, docker)
                result = zone.validate_legacy_zone(snapshot(environment), 'UTC', CID, docker)
                self.assertEqual(result['proof']['configured']['status'], 'matches')
                self.assertEqual(result['origin'], 'operator-confirmed')

    def test_consistent_configuration_mismatch_refuses_even_confirmation(self):
        for environment in ({'TZ': 'UTC'}, {'JAVA_TOOL_OPTIONS': '-Duser.timezone=UTC'},
                            {'TZ': 'UTC', 'JAVA_OPTS': '-Duser.timezone=Etc/UTC'}):
            with self.subTest(source=list(environment)), self.assertRaisesRegex(RuntimeError, 'configuration differs'):
                zone.validate_legacy_zone(snapshot(environment), 'Asia/Seoul', CID,
                                          DockerProbe('ieum-zone-v1|unavailable|missing_tools'))

    def test_ambiguous_configuration_is_not_inferred_but_may_be_attested(self):
        for environment in ({'TZ': 'UTC', 'JAVA_OPTS': '-Duser.timezone=Asia/Seoul'},
                            {'JAVA_TOOL_OPTIONS': '-Duser.timezone=UTC -Duser.timezone=Asia/Seoul'},
                            {'JAVA_TOOL_OPTIONS': '-Duser.timezone=' + SECRET},
                            {'JAVA_OPTS': '-Duser.timezone "unterminated'}, {'TZ': '/etc/localtime'}):
            with self.subTest(source=list(environment)):
                docker = DockerProbe('ieum-zone-v1|unavailable|missing_tools')
                with self.assertRaisesRegex(RuntimeError, 'exact captured container ID'):
                    zone.validate_legacy_zone(snapshot(environment), 'Asia/Seoul', None, docker)
                result = zone.validate_legacy_zone(snapshot(environment), 'Asia/Seoul', CID, docker)
                self.assertEqual(result['proof']['configured']['status'], 'ambiguous')
                self.assertNotIn(SECRET, json.dumps(result))

    def test_wrong_or_short_confirmation_never_applies_to_another_container(self):
        for confirmation in ('a' * 12, 'c' * 64, True, ''):
            docker = DockerProbe()
            with self.subTest(confirmation=confirmation), self.assertRaisesRegex(RuntimeError, 'exact captured 64-character'):
                zone.validate_legacy_zone(snapshot(), 'UTC', confirmation, docker)
            self.assertEqual(docker.calls, [])

    def test_invalid_requested_zone_and_identity_fail_before_docker(self):
        for requested in ('Asia/Seoul ', 'asia/seoul', 'localtime', 'UTC+09', None, SECRET):
            docker = DockerProbe()
            with self.subTest(requested=requested), self.assertRaisesRegex(RuntimeError, 'reviewed IANA'):
                zone.validate_legacy_zone(snapshot(), requested, None, docker)
            self.assertEqual(docker.calls, [])
        for changes in ({'containerId': None}, {'containerId': 'short'}, {'image': ''},
                        {'startedAt': ''}, {'startedAt': None}, {'runtimeEnvironment': {'TZ': None}}):
            value = snapshot(); value.update(changes)
            docker = DockerProbe()
            with self.subTest(changes=changes), self.assertRaisesRegex(RuntimeError, 'pinned container ID'):
                zone.validate_legacy_zone(value, 'UTC', None, docker)
            self.assertEqual(docker.calls, [])

    def test_changed_identity_or_stopped_container_cannot_be_confirmed(self):
        for change in ({'id': 'c' * 64}, {'image': 'other-image'}, {'status': 'exited'}):
            changed = identity(); changed.update(change)
            docker = DockerProbe(identities=[changed])
            with self.subTest(change=change), self.assertRaisesRegex(RuntimeError, 'identity changed'):
                zone.validate_legacy_zone(snapshot(), 'UTC', CID, docker)
            self.assertEqual(len(docker.calls), 1)

    def test_restart_after_snapshot_is_rejected_before_jcmd_probe(self):
        changed = identity(); changed['startedAt'] = '2026-10-21T00:00:00Z'
        docker = DockerProbe(identities=[changed])
        with self.assertRaisesRegex(RuntimeError, 'restarted after the captured source snapshot'):
            zone.validate_legacy_zone(snapshot(), 'UTC', CID, docker)
        self.assertEqual(len(docker.calls), 1)
        self.assertEqual(docker.calls[0][0][0], 'inspect')

    def test_restart_during_probe_rejects_even_when_same_id_image_and_zone(self):
        changed = identity(); changed['startedAt'] = '2026-10-21T00:00:00Z'
        docker = DockerProbe(identities=[identity(), changed])
        with self.assertRaisesRegex(RuntimeError, 'restarted or changed'):
            zone.validate_legacy_zone(snapshot(), 'UTC', CID, docker)

    def test_identity_errors_never_leak_docker_error_properties_or_stderr(self):
        docker = DockerProbe(identities=[RuntimeError(SECRET)])
        with self.assertRaisesRegex(RuntimeError, 'could not be verified') as failure:
            zone.validate_legacy_zone(snapshot(), 'UTC', CID, docker)
        self.assertNotIn(SECRET, str(failure.exception))
        self.assertTrue(failure.exception.__suppress_context__)

    def test_probe_exception_nonstring_and_unrecognized_output_cannot_be_attested_or_leak_secrets(self):
        for output in (RuntimeError(SECRET), TimeoutError(SECRET), None, b'ieum-zone-v1|actual|17|UTC',
                       'user.password=' + SECRET, 'x' * 32769, '', 'ieum-zone-v1|actual|17|' + SECRET):
            with self.subTest(kind=type(output).__name__), self.assertRaisesRegex(RuntimeError, 'process changed or returned incomplete') as failure:
                zone.validate_legacy_zone(snapshot(), 'UTC', CID, DockerProbe(output))
            self.assertNotIn(SECRET, str(failure.exception))

    def test_probe_timeout_or_partial_stream_cannot_bypass_known_os_evidence_with_confirmation(self):
        full = startup_probe('ieum-zone-v2|input|etc-timezone|zone|UTC')
        partial = full[:full.index('ieum-zone-v2|input|etc-timezone')]
        for output in (TimeoutError('simulated probe timeout'), partial, full[:40]):
            with self.subTest(kind=type(output).__name__), self.assertRaisesRegex(RuntimeError, 'process changed or returned incomplete'):
                zone.validate_legacy_zone(snapshot(), 'Asia/Seoul', CID, DockerProbe(output))

    def test_multiple_jvms_and_empty_property_need_attestation_not_arbitrary_pid_selection(self):
        for reason in ('ambiguous_jvm', 'missing_or_unknown_property', 'attach_failed'):
            with self.subTest(reason=reason), self.assertRaisesRegex(RuntimeError, 'unavailable'):
                zone.validate_legacy_zone(snapshot(), 'UTC', None, DockerProbe('ieum-zone-v1|unavailable|' + reason))

    def test_probe_shell_is_valid_and_only_existing_jcmd_is_requested(self):
        subprocess.run(['sh', '-n'], input=zone.PROBE_SCRIPT, text=True, check=True, capture_output=True)
        self.assertIn('timeout 5s jcmd "$pid" VM.system_properties 2>/dev/null', zone.PROBE_SCRIPT)
        self.assertNotIn('java -XshowSettings', zone.PROBE_SCRIPT)
        self.assertNotIn('jcmd -l', zone.PROBE_SCRIPT)
        self.assertNotIn('apt', zone.PROBE_SCRIPT)

    def test_in_container_filter_never_outputs_other_properties_and_rejects_failed_attach(self):
        for text, expected in (
            ('17:\npassword=' + SECRET + '\nuser.timezone=UTC\n__IEUM_JCMD_STATUS__=0\n', 'ieum-zone-v1|actual|17|UTC'),
            ('user.timezone=UTC\npassword=' + SECRET + '\n__IEUM_JCMD_STATUS__=124\n', 'ieum-zone-v1|unavailable|attach_failed'),
            ('user.timezone=UTC\nuser.timezone=Asia/Seoul\n__IEUM_JCMD_STATUS__=0\n', 'ieum-zone-v1|unavailable|missing_or_unknown_property'),
            ('user.timezone=' + SECRET + '\n__IEUM_JCMD_STATUS__=0\n', 'ieum-zone-v1|unavailable|missing_or_unknown_property')):
            with self.subTest(expected=expected):
                result = subprocess.run(['awk', '-v', 'allowed=UTC Asia/Seoul', '-v', 'pid=17', zone.PROPERTY_FILTER],
                                        input=text, text=True, check=True, capture_output=True)
                self.assertEqual(result.stdout.strip(), expected)
                self.assertNotIn(SECRET, result.stdout + result.stderr)

    def test_jre_missing_jcmd_etc_utc_cannot_be_confirmed_as_seoul(self):
        output = startup_probe('ieum-zone-v2|input|etc-timezone|zone|Etc/UTC')
        for confirmation in (None, CID):
            with self.subTest(confirmation=confirmation), self.assertRaisesRegex(RuntimeError, 'startup evidence differs'):
                zone.validate_legacy_zone(snapshot(), 'Asia/Seoul', confirmation, DockerProbe(output))

    def test_matching_startup_files_need_attestation_and_do_not_claim_runtime_proof(self):
        output = startup_probe('ieum-zone-v2|input|etc-timezone|zone|Etc/UTC',
                               'ieum-zone-v2|input|etc-localtime|zone|UTC')
        with self.assertRaisesRegex(RuntimeError, 'exact captured container ID'):
            zone.validate_legacy_zone(snapshot(), 'UTC', None, DockerProbe(output))
        result = zone.validate_legacy_zone(snapshot(), 'UTC', CID, DockerProbe(output))
        self.assertEqual(result['origin'], 'operator-confirmed')
        self.assertEqual(result['proof']['pid'], 17)
        self.assertEqual(result['proof']['pidStartTicks'], '123')
        startup = result['proof']['startupInputs']
        self.assertEqual(startup['status'], 'matches')
        self.assertIn('do not prove cached JVM state', startup['limitation'])

    def test_conflicting_known_os_inputs_refuse_confirmation_for_either_jdk21_order(self):
        output = startup_probe('ieum-zone-v2|input|etc-timezone|zone|UTC',
                               'ieum-zone-v2|input|etc-localtime|zone|Asia/Seoul')
        for requested in ('UTC', 'Asia/Seoul'):
            with self.subTest(requested=requested), self.assertRaisesRegex(RuntimeError, 'startup evidence differs'):
                zone.validate_legacy_zone(snapshot(), requested, CID, DockerProbe(output))

    def test_process_property_precedes_tz_and_platform_but_matching_still_needs_attestation(self):
        output = startup_probe('ieum-zone-v2|input|cmdline|zone|Asia/Seoul',
                               'ieum-zone-v2|input|TZ|zone|UTC',
                               'ieum-zone-v2|input|etc-timezone|zone|UTC',
                               'ieum-zone-v2|input|etc-localtime|zone|UTC')
        result = zone.validate_legacy_zone(snapshot({'TZ': 'UTC'}), 'Asia/Seoul', CID, DockerProbe(output))
        self.assertEqual(result['proof']['startupInputs']['basis'], 'jvm-startup-properties')
        self.assertEqual(result['proof']['startupInputs']['zones'], ['Asia/Seoul'])
        with self.assertRaisesRegex(RuntimeError, 'startup evidence differs'):
            zone.validate_legacy_zone(snapshot(), 'UTC', CID, DockerProbe(output))

    def test_process_tz_precedes_os_and_config_env_and_ignored_java_opts(self):
        output = startup_probe('ieum-zone-v2|input|TZ|zone|Asia/Seoul',
                               'ieum-zone-v2|input|etc-timezone|zone|UTC')
        environment = {'TZ': 'UTC', 'JAVA_OPTS': '-Duser.timezone=UTC'}
        result = zone.validate_legacy_zone(snapshot(environment), 'Asia/Seoul', CID, DockerProbe(output))
        self.assertEqual(result['proof']['startupInputs']['basis'], 'process-TZ')
        self.assertEqual(result['proof']['configured']['status'], 'mismatch')

    def test_conflicting_property_inputs_are_not_resolved_using_guessed_environment_order(self):
        for source in ('JAVA_TOOL_OPTIONS', 'JDK_JAVA_OPTIONS', '_JAVA_OPTIONS', 'cmdline'):
            output = startup_probe('ieum-zone-v2|input|cmdline|zone|Asia/Seoul',
                                   f'ieum-zone-v2|input|{source}|zone|UTC')
            for requested in ('UTC', 'Asia/Seoul'):
                with self.subTest(source=source, requested=requested), self.assertRaisesRegex(RuntimeError, 'startup evidence differs'):
                    zone.validate_legacy_zone(snapshot(), requested, CID, DockerProbe(output))

    def test_custom_gmt_property_cannot_be_confirmed_as_region_or_another_offset(self):
        for source in ('cmdline', 'JAVA_TOOL_OPTIONS', 'TZ'):
            output = startup_probe(f'ieum-zone-v2|input|{source}|zone|GMT+09:00',
                                   'ieum-zone-v2|input|etc-timezone|zone|UTC')
            for requested in ('UTC', 'Asia/Seoul'):
                with self.subTest(source=source, requested=requested), self.assertRaisesRegex(RuntimeError, 'startup evidence differs'):
                    zone.validate_legacy_zone(snapshot(), requested, CID, DockerProbe(output))
            result = zone.validate_legacy_zone(snapshot(), 'Etc/GMT-9', CID, DockerProbe(output))
            self.assertEqual(result['proof']['startupInputs']['status'], 'matches')
        result = filter_proc(b'java\0-Duser.timezone=GMT+09:00\0-jar\0app.jar\0', 'cmdline')
        self.assertIn(b'ieum-zone-v2|input|cmdline|zone|GMT+09:00', result.stdout)
        result = filter_proc(b'JAVA_TOOL_OPTIONS=-Duser.timezone=GMT+99:99\0', 'environment')
        self.assertIn(b'ieum-zone-v2|input|JAVA_TOOL_OPTIONS|unsupported', result.stdout)

    def test_unknown_input_cannot_hide_other_known_possible_mismatch(self):
        output = startup_probe('ieum-zone-v2|input|TZ|unsupported',
                               'ieum-zone-v2|input|cmdline|unsupported',
                               'ieum-zone-v2|input|etc-timezone|zone|UTC')
        with self.assertRaisesRegex(RuntimeError, 'startup evidence differs'):
            zone.validate_legacy_zone(snapshot(), 'Asia/Seoul', CID, DockerProbe(output))

    def test_actual_jcmd_property_remains_strongest_when_startup_files_disagree(self):
        output = startup_probe('ieum-zone-v2|input|etc-timezone|zone|Asia/Seoul',
                               actual='ieum-zone-v1|actual|17|UTC')
        result = zone.validate_legacy_zone(snapshot(), 'UTC', None, DockerProbe(output))
        self.assertEqual(result['origin'], 'running-jvm')

    def test_tzif_hash_supports_full_definition_match_without_guessing_offset(self):
        digest = zone.hashlib.sha256(zone._zone_bytes('UTC')).hexdigest()
        output = startup_probe('ieum-zone-v2|localtime-sha256|' + digest)
        result = zone.validate_legacy_zone(snapshot(), 'UTC', CID, DockerProbe(output))
        self.assertTrue(result['proof']['startupInputs']['requestedTzifDigestMatches'])
        self.assertEqual(result['proof']['startupInputs']['status'], 'matches')
        result = zone.validate_legacy_zone(snapshot(), 'Asia/Seoul', CID, DockerProbe(output))
        self.assertFalse(result['proof']['startupInputs']['requestedTzifDigestMatches'])
        self.assertEqual(result['proof']['startupInputs']['status'], 'unknown')

    def test_v2_pid_change_duplicate_identity_and_truncated_evidence_refuse_confirmation(self):
        valid = startup_probe()
        invalid = [valid.replace('ieum-zone-v2|complete', 'ieum-zone-v2|invalid|pid_changed'),
                   'ieum-zone-v2|' + ('x' * 32768),
                   valid + '\nieum-zone-v2|process|18|124',
                   valid.replace('ieum-zone-v2|read|cmdline|ok', 'ieum-zone-v2|process|18|124'),
                   valid.replace('ieum-zone-v2|complete', ''),
                   valid.replace('|process|17|123', '|process|17|0'),
                   valid.replace('|localtime-sha256|unavailable', '|localtime-sha256|' + SECRET),
                   startup_probe(actual='ieum-zone-v1|actual|18|UTC')]
        for output in invalid:
            with self.subTest(case=invalid.index(output)), self.assertRaisesRegex(RuntimeError, 'process changed or returned incomplete') as failure:
                zone.validate_legacy_zone(snapshot(), 'UTC', CID, DockerProbe(output))
            self.assertNotIn(SECRET, str(failure.exception))

    def test_nul_environment_filter_does_not_parse_newline_secret_as_new_environment_entry(self):
        payload = ('OTHER=' + SECRET + '\nTZ=Asia/Seoul\n-Duser.timezone=Asia/Seoul\0TZ=UTC\0'
                   'JAVA_TOOL_OPTIONS=-Dpassword=' + SECRET + ' -Duser.timezone="Etc/UTC"\0').encode()
        result = filter_proc(payload, 'environment')
        self.assertEqual(result.stdout.decode().splitlines(), [
            'ieum-zone-v2|input|TZ|zone|UTC',
            'ieum-zone-v2|input|JAVA_TOOL_OPTIONS|zone|Etc/UTC',
            'ieum-zone-v2|read|environment|ok'])
        self.assertNotIn(SECRET.encode(), result.stdout + result.stderr)

    def test_nul_cmdline_filter_stops_at_application_target_and_skips_launcher_operands(self):
        for target in (['-jar', 'application.jar'], ['-m', 'app/main'], ['--module', 'app/main'], ['com.ieum.Main']):
            payload = '\0'.join(['java', '-cp', '-Duser.timezone=Asia/Seoul', '-Dpassword=' + SECRET,
                                 '-Duser.timezone=UTC', *target, '-Duser.timezone=Asia/Seoul']) + '\0'
            result = filter_proc(payload.encode(), 'cmdline')
            with self.subTest(target=target):
                self.assertEqual(result.stdout.decode().splitlines(), ['ieum-zone-v2|input|cmdline|zone|UTC',
                                                                      'ieum-zone-v2|read|cmdline|ok'])
                self.assertNotIn(SECRET.encode(), result.stdout + result.stderr)

    def test_source_launcher_operand_is_not_mistaken_for_application_target(self):
        payload = b'\0'.join([b'java', b'--source', b'21', b'-Duser.timezone=Asia/Seoul',
                              b'Main.java', b'-Duser.timezone=UTC']) + b'\0'
        result = filter_proc(payload, 'cmdline')
        self.assertEqual(result.stdout.decode().splitlines(), ['ieum-zone-v2|input|cmdline|zone|Asia/Seoul',
                                                               'ieum-zone-v2|read|cmdline|ok'])

    def test_failed_missing_or_duplicate_proc_read_status_rejects_partial_evidence(self):
        for payload, status in ((b'TZ=UTC\0', '__IEUM_PROC_READ_EXIT__=1\n'),
                                (b'TZ=UTC\0', ''), (b'', '__IEUM_PROC_READ_EXIT__=5\n'),
                                (b'TZ=UTC\0', '__IEUM_PROC_READ_EXIT__=0\n__IEUM_PROC_READ_EXIT__=0\n')):
            output = filter_proc(payload, 'environment', status).stdout.decode()
            self.assertIn('ieum-zone-v2|invalid|', output)
            probe = startup_probe(*output.splitlines())
            with self.subTest(status=status), self.assertRaisesRegex(RuntimeError, 'process changed or returned incomplete'):
                zone.validate_legacy_zone(snapshot(), 'Asia/Seoul', CID, DockerProbe(probe))

    def test_empty_cmdline_cannot_be_reported_as_successful_proc_observation(self):
        result = filter_proc(b'', 'cmdline')
        self.assertIn(b'ieum-zone-v2|invalid|empty_cmdline', result.stdout)

    def test_shell_preserves_od_failure_after_partial_valid_zone_bytes(self):
        # Simulate od writing a valid TZ=UTC record and then reporting an I/O error.
        command = "{ sh -c 'printf \"84 90 61 85 84 67 0\\n\"; exit 5'; printf '__IEUM_PROC_READ_EXIT__=%s\\n' \"$?\"; }"
        result = subprocess.run(['sh', '-c', command, 'ieum-test'],
                                check=True, capture_output=True, text=True)
        filtered = subprocess.run(['awk', '-v', 'allowed=UTC', '-v', 'mode=environment', zone.INPUT_FILTER],
                                  input=result.stdout, text=True, check=True, capture_output=True)
        self.assertIn('ieum-zone-v2|input|TZ|zone|UTC', filtered.stdout)
        self.assertIn('ieum-zone-v2|invalid|proc_read_failed', filtered.stdout)
        with self.assertRaisesRegex(RuntimeError, 'process changed or returned incomplete'):
            zone.validate_legacy_zone(snapshot(), 'UTC', CID, DockerProbe(startup_probe(*filtered.stdout.splitlines())))

    def test_bad_options_argument_files_and_oversized_proc_input_emit_only_fixed_flags(self):
        payloads = [('environment', 'JAVA_TOOL_OPTIONS=-Duser.timezone="' + SECRET + '\0'),
                    ('environment', 'TZ=' + SECRET + '\0'),
                    ('cmdline', 'java\0@' + SECRET + '\0-jar\0application.jar\0'),
                    ('environment', 'OTHER=' + SECRET + ('x' * 131072) + '\0')]
        for mode, payload in payloads:
            result = filter_proc(payload.encode(), mode)
            with self.subTest(mode=mode, length=len(payload)):
                self.assertRegex(result.stdout.decode(), r'ieum-zone-v2\|(input\|[^|]+\|unsupported|invalid\|input_too_large)')
                self.assertNotIn(SECRET.encode(), result.stdout + result.stderr)

    def test_unterminated_proc_record_and_overlong_v2_stream_cannot_be_attested(self):
        output = filter_proc(b'TZ=UTC', 'environment').stdout.decode()
        self.assertIn('ieum-zone-v2|invalid|unterminated_proc_input', output)
        output = startup_probe(*(['ieum-zone-v2|input|cmdline|unsupported'] * 161))
        with self.assertRaisesRegex(RuntimeError, 'process changed or returned incomplete'):
            zone.validate_legacy_zone(snapshot(), 'UTC', CID, DockerProbe(output))

    def test_jre_shell_probe_reads_synthetic_proc_and_os_files_without_jcmd_or_docker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            process = root / 'proc/17'; process.mkdir(parents=True)
            (process / 'comm').write_text('java\n')
            (process / 'stat').write_text('17 (java) S ' + ('0 ' * 18) + '123\n')
            (process / 'environ').write_bytes(('OTHER=' + SECRET + '\nTZ=Asia/Seoul\0').encode())
            (process / 'cmdline').write_bytes(('java\0-Dpassword=' + SECRET + '\0-jar\0application.jar\0').encode())
            (root / 'timezone').write_text('Etc/UTC\n')
            (root / 'localtime').symlink_to('/usr/share/zoneinfo/Etc/UTC')
            script = zone.PROBE_SCRIPT.replace('/proc/', str(root / 'proc') + '/')
            script = script.replace('/etc/timezone', str(root / 'timezone')).replace('/etc/localtime', str(root / 'localtime'))
            script = script.replace('if command -v jcmd >/dev/null 2>&1 && command -v timeout >/dev/null 2>&1; then', 'if false; then')
            result = subprocess.run(['sh', '-c', script, 'ieum-test', 'UTC Etc/UTC Asia/Seoul'],
                                    check=True, capture_output=True, text=True)
            self.assertNotIn(SECRET, result.stdout + result.stderr)
            self.assertIn('ieum-zone-v2|input|etc-timezone|zone|Etc/UTC', result.stdout)
            with self.assertRaisesRegex(RuntimeError, 'startup evidence differs'):
                zone.validate_legacy_zone(snapshot(), 'Asia/Seoul', CID, DockerProbe(result.stdout))


if __name__ == '__main__':
    unittest.main()
