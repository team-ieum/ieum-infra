import importlib.util
import json
import os
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
        value = self.probe.pop(0) if isinstance(self.probe, list) else self.probe
        if isinstance(value, Exception):
            raise value
        return value


def identity(**changes):
    return dict(id=CID, image=IMAGE, status='running', startedAt='2026-10-20T00:00:00Z', **changes)


def startup_probe(*records, actual='ieum-zone-v1|unavailable|missing_tools', pid=17, ticks=123,
                  environment_read='ok', cmdline_read='ok', localtime_resolution=None):
    if localtime_resolution is None:
        localtime_resolution = ('symlink' if any('|etc-localtime|zone|' in item for item in records)
                                else 'unresolved' if any('|etc-localtime|unsupported' in item for item in records)
                                else 'missing')
    return '\n'.join([f'ieum-zone-v2|process|{pid}|{ticks}', actual,
                      'ieum-zone-v2|read|environment|' + environment_read,
                      'ieum-zone-v2|read|cmdline|' + cmdline_read,
                      *records,
                      *([] if any('|etc-timezone|' in item for item in records)
                        else ['ieum-zone-v2|input|etc-timezone|missing']),
                      *([] if any('|etc-localtime|' in item for item in records)
                        else ['ieum-zone-v2|input|etc-localtime|missing']),
                      *([] if any('|localtime-sha256|' in item for item in records)
                        else ['ieum-zone-v2|localtime-sha256|unavailable']),
                      *([] if any('|localtime-resolution|' in item for item in records)
                        else ['ieum-zone-v2|localtime-resolution|' + localtime_resolution]),
                      'ieum-zone-v2|complete'])


def reviewed_zone(source, requested, confirmation, docker, **kwargs):
    """The old success cases explicitly review the new challenge before retrying."""
    try:
        return zone.validate_legacy_zone(source, requested, confirmation, docker, **kwargs)
    except zone.LegacyConfirmationRequired as challenge:
        if confirmation is None:
            raise
        return zone.validate_legacy_zone(source, requested, confirmation, docker,
                                         confirmation_evidence=challenge.fingerprint, **kwargs)


def filter_proc(payload, mode, status='__IEUM_PROC_READ_EXIT__=0\n'):
    encoded = subprocess.run(['od', '-An', '-v', '-tu1'], input=payload, check=True, capture_output=True)
    return subprocess.run(['awk', '-v', 'allowed=UTC Etc/UTC Asia/Seoul', '-v', 'mode=' + mode, zone.INPUT_FILTER],
                          input=encoded.stdout + status.encode(), check=True, capture_output=True)


class LegacyTimeZoneTest(unittest.TestCase):
    def confirmation(self, source, requested, output, **kwargs):
        with self.assertRaises(zone.LegacyConfirmationRequired) as failure:
            zone.validate_legacy_zone(source, requested, CID, DockerProbe(output), **kwargs)
        return failure.exception

    def resolver(self, local_bytes, zone_bytes, script=None, environment=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'localtime').write_bytes(local_bytes)
            for name, payload in zone_bytes.items():
                path = root / 'zoneinfo' / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
            return subprocess.run(['sh', '-c', script or zone.LOCALTIME_RESOLVER_SCRIPT, 'ieum-test',
                                   ' '.join(sorted(zone_bytes)), str(root / 'localtime'), str(root / 'zoneinfo')],
                                  check=False, capture_output=True, text=True, timeout=5, env=environment)

    def test_actual_jvm_matches_without_confirmation_and_is_pinned_before_and_after(self):
        docker = DockerProbe()
        result = reviewed_zone(snapshot({'PASSWORD': SECRET}), 'UTC', None, docker)
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
        result = reviewed_zone(snapshot({'TZ': 'Asia/Seoul'}), 'UTC', None, DockerProbe())
        self.assertEqual(result['origin'], 'running-jvm')
        self.assertEqual(result['proof']['configured']['status'], 'mismatch')

    def test_full_rule_alias_is_accepted_but_same_current_offset_is_not(self):
        result = reviewed_zone(snapshot(), 'Etc/UTC', None, DockerProbe())
        self.assertEqual(result['effectiveZone'], 'UTC')
        with self.assertRaisesRegex(RuntimeError, 'running JVM time zone differs'):
            zone.validate_legacy_zone(snapshot(), 'Etc/GMT-9', CID, DockerProbe('ieum-zone-v1|actual|17|Asia/Seoul'))

    def test_known_fixed_jvm_offset_cannot_be_confirmed_as_region_rules(self):
        with self.assertRaisesRegex(RuntimeError, 'running JVM time zone differs'):
            zone.validate_legacy_zone(snapshot(), 'Asia/Seoul', CID, DockerProbe('ieum-zone-v1|actual|17|GMT+09:00'))
        result = reviewed_zone(snapshot(), 'UTC', None, DockerProbe('ieum-zone-v1|actual|17|GMT+00:00'))
        self.assertEqual(result['origin'], 'running-jvm')

    def test_missing_jcmd_and_no_tz_never_implies_image_default_utc(self):
        docker = DockerProbe('ieum-zone-v1|unavailable|missing_tools')
        with self.assertRaisesRegex(RuntimeError, 'exact captured container ID'):
            zone.validate_legacy_zone(snapshot(), 'Asia/Seoul', None, docker)
        result = reviewed_zone(snapshot(), 'Asia/Seoul', CID, docker)
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
                result = reviewed_zone(snapshot(environment), 'UTC', CID, docker)
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
                result = reviewed_zone(snapshot(environment), 'Asia/Seoul', CID, docker)
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

    def test_unread_environment_makes_os_mismatch_ambiguous_but_requires_exact_confirmation(self):
        output = startup_probe('ieum-zone-v2|input|etc-timezone|zone|Etc/UTC',
                               environment_read='unavailable')
        with self.assertRaisesRegex(RuntimeError, 'exact captured container ID'):
            zone.validate_legacy_zone(snapshot({'TZ': 'Asia/Seoul'}), 'Asia/Seoul', None, DockerProbe(output))
        result = reviewed_zone(snapshot({'TZ': 'Asia/Seoul'}), 'Asia/Seoul', CID, DockerProbe(output))
        self.assertEqual(result['origin'], 'operator-confirmed')
        startup = result['proof']['startupInputs']
        self.assertEqual(startup['status'], 'ambiguous')
        self.assertFalse(startup['processInputsComplete'])
        self.assertEqual(startup['zones'], ['Etc/UTC'])
        self.assertEqual(result['proof']['configured']['status'], 'matches')

    def test_unread_cmdline_can_override_process_tz_and_os_inputs(self):
        output = startup_probe('ieum-zone-v2|input|TZ|zone|UTC',
                               'ieum-zone-v2|input|etc-timezone|zone|UTC', cmdline_read='unavailable')
        result = reviewed_zone(snapshot({'JAVA_TOOL_OPTIONS': '-Duser.timezone=Asia/Seoul'}),
                                           'Asia/Seoul', CID, DockerProbe(output))
        self.assertEqual(result['proof']['startupInputs']['basis'], 'process-TZ')
        self.assertEqual(result['proof']['startupInputs']['status'], 'ambiguous')

    def test_unread_environment_may_contain_a_property_overriding_known_cmdline_property(self):
        output = startup_probe('ieum-zone-v2|input|cmdline|zone|UTC', environment_read='unavailable')
        result = reviewed_zone(snapshot({'_JAVA_OPTIONS': '-Duser.timezone=Asia/Seoul'}),
                                           'Asia/Seoul', CID, DockerProbe(output))
        self.assertEqual(result['proof']['startupInputs']['basis'], 'jvm-startup-properties')
        self.assertEqual(result['proof']['startupInputs']['status'], 'ambiguous')

    def test_partial_reads_cannot_confirm_any_known_config_env_mismatch(self):
        output = startup_probe('ieum-zone-v2|input|etc-timezone|zone|UTC', environment_read='unavailable')
        for environment in ({'TZ': 'UTC'}, {'JAVA_TOOL_OPTIONS': '-Duser.timezone=UTC'},
                            {'TZ': 'Asia/Seoul', 'JAVA_OPTS': '-Duser.timezone=UTC'},
                            {'TZ': 'UTC', 'JAVA_TOOL_OPTIONS': '-Duser.timezone=' + SECRET}):
            docker = DockerProbe(output)
            with self.subTest(source=list(environment)), self.assertRaisesRegex(RuntimeError, 'configuration differs') as failure:
                zone.validate_legacy_zone(snapshot(environment), 'Asia/Seoul', CID, docker)
            self.assertNotIn(SECRET, str(failure.exception))
            self.assertEqual(len([call for call in docker.calls if call[0][0] == 'exec']), 1)

    def test_actual_property_overrides_partial_inputs_and_config_env_hints(self):
        output = startup_probe('ieum-zone-v2|input|etc-timezone|zone|Asia/Seoul',
                               environment_read='unavailable', actual='ieum-zone-v1|actual|17|UTC')
        result = reviewed_zone(snapshot({'TZ': 'Asia/Seoul'}), 'UTC', None, DockerProbe(output))
        self.assertEqual(result['origin'], 'running-jvm')

    def test_partial_reads_treat_known_custom_config_gmt_as_rules_not_unknown_strings(self):
        output = startup_probe('ieum-zone-v2|input|etc-timezone|zone|UTC', environment_read='unavailable')
        for environment in ({'TZ': 'GMT+09:00'}, {'JAVA_TOOL_OPTIONS': '-Duser.timezone=GMT+09:00'},
                            {'TZ': 'UTC', '_JAVA_OPTIONS': '-Duser.timezone=GMT+09:00'},
                            {'JAVA_OPTS': '-Duser.timezone=GMT+05:30'}):
            with self.subTest(source=list(environment)), self.assertRaisesRegex(RuntimeError, 'configuration differs'):
                zone.validate_legacy_zone(snapshot(environment), 'UTC', CID, DockerProbe(output))
        for environment in ({'TZ': ':GMT+00:00'}, {'JAVA_TOOL_OPTIONS': '-Duser.timezone=GMT-00:00'},
                            {'TZ': 'UTC', '_JAVA_OPTIONS': '-Duser.timezone=GMT+00:00'}):
            result = reviewed_zone(snapshot(environment), 'UTC', CID, DockerProbe(output))
            self.assertEqual(result['proof']['configured']['status'], 'matches')
        result = reviewed_zone(snapshot({'JAVA_TOOL_OPTIONS': '-Duser.timezone=GMT+09:00'}),
                                           'Etc/GMT-9', CID, DockerProbe(output))
        self.assertEqual(result['proof']['configured']['status'], 'matches')
        self.assertEqual(result['proof']['startupInputs']['status'], 'ambiguous')

    def test_partial_reads_without_any_zone_are_ambiguous_instead_of_runtime_proof(self):
        result = reviewed_zone(snapshot(), 'UTC', CID,
                                           DockerProbe(startup_probe(cmdline_read='unavailable')))
        self.assertEqual(result['proof']['startupInputs']['status'], 'ambiguous')
        self.assertEqual(result['proof']['startupInputs']['zones'], [])

    def test_invalid_probe_settings_fail_before_docker(self):
        for timeout in (None, True, 5, 61, 10.0, float('inf'), float('nan'), SECRET):
            docker = DockerProbe()
            with self.subTest(timeout=timeout), self.assertRaisesRegex(RuntimeError, 'timeout must be an integer'):
                zone.validate_legacy_zone(snapshot(), 'UTC', None, docker, probe_timeout=timeout)
            self.assertEqual(docker.calls, [])
        for attempts in (None, False, 0, 4, 2.0, SECRET):
            docker = DockerProbe()
            with self.subTest(attempts=attempts), self.assertRaisesRegex(RuntimeError, 'attempts must be an integer'):
                zone.validate_legacy_zone(snapshot(), 'UTC', None, docker, probe_attempts=attempts)
            self.assertEqual(docker.calls, [])

    def test_probe_timeout_retries_then_records_sanitized_success_with_custom_timeout(self):
        docker = DockerProbe([subprocess.TimeoutExpired(SECRET, 37, output=SECRET, stderr=SECRET),
                              'ieum-zone-v1|actual|17|UTC'])
        result = reviewed_zone(snapshot(), 'UTC', None, docker, probe_timeout=37)
        self.assertEqual(result['proof']['probe'], {'code': 'probe_ok', 'attempts': 2, 'timeoutSeconds': 37})
        self.assertEqual([call[0][0] for call in docker.calls], ['inspect', 'exec', 'exec', 'inspect'])
        self.assertEqual([timeout for args, timeout in docker.calls if args[0] == 'exec'], [37, 37])
        self.assertNotIn(SECRET, json.dumps(result))

    def test_transport_retry_obeys_explicit_max_attempts_and_does_not_relaunch_java(self):
        docker = DockerProbe([ConnectionError(SECRET), ConnectionResetError(SECRET),
                              'ieum-zone-v1|actual|17|UTC'])
        result = reviewed_zone(snapshot(), 'UTC', None, docker, probe_attempts=3, probe_timeout=60)
        self.assertEqual(result['proof']['probe'], {'code': 'probe_ok', 'attempts': 3, 'timeoutSeconds': 60})
        self.assertEqual(len([call for call in docker.calls if call[0][0] == 'exec']), 3)
        self.assertTrue(all('java -XshowSettings' not in str(args) for args, _ in docker.calls))

    def test_retry_exhaustion_has_fixed_code_attempts_timeout_and_never_allows_confirmation(self):
        for exception, code in ((TimeoutError(SECRET), 'probe_timeout'),
                                (ConnectionError(SECRET), 'probe_transport_failure')):
            docker = DockerProbe(exception)
            with self.subTest(code=code), self.assertRaises(zone.LegacyProbeError) as failure:
                zone.validate_legacy_zone(snapshot(), 'UTC', CID, docker)
            self.assertEqual(failure.exception.code, code)
            self.assertEqual(failure.exception.attempts, 2)
            self.assertEqual(failure.exception.timeoutSeconds, 10)
            self.assertEqual(len([call for call in docker.calls if call[0][0] == 'exec']), 2)
            self.assertTrue(failure.exception.__suppress_context__)
            self.assertNotIn(SECRET, str(failure.exception))

    def test_one_attempt_setting_is_respected_at_timeout_lower_bound(self):
        docker = DockerProbe(TimeoutError(SECRET))
        with self.assertRaises(zone.LegacyProbeError) as failure:
            zone.validate_legacy_zone(snapshot(), 'UTC', CID, docker, probe_timeout=6, probe_attempts=1)
        self.assertEqual((failure.exception.code, failure.exception.attempts, failure.exception.timeoutSeconds),
                         ('probe_timeout', 1, 6))
        self.assertEqual(len([call for call in docker.calls if call[0][0] == 'exec']), 1)

    def test_generic_or_nonzero_command_failure_is_not_retried_and_diagnostics_are_fixed(self):
        for exception in (RuntimeError(SECRET), OSError(SECRET),
                          subprocess.CalledProcessError(125, SECRET, output=SECRET, stderr=SECRET)):
            docker = DockerProbe(exception)
            with self.subTest(kind=type(exception).__name__), self.assertRaises(zone.LegacyProbeError) as failure:
                zone.validate_legacy_zone(snapshot(), 'UTC', CID, docker, probe_attempts=3)
            self.assertEqual(failure.exception.code, 'probe_command_failed')
            self.assertEqual(failure.exception.attempts, 1)
            self.assertEqual(len([call for call in docker.calls if call[0][0] == 'exec']), 1)
            self.assertNotIn(SECRET, str(failure.exception))

    def test_malformed_and_changed_process_results_are_not_retried(self):
        outputs = [(None, 'probe_invalid_result'), (SECRET, 'probe_invalid_result'),
                   ('x' * 32769, 'probe_invalid_result'),
                   (startup_probe().replace('ieum-zone-v2|complete', 'ieum-zone-v2|invalid|pid_changed'),
                    'probe_incomplete_or_changed_process')]
        for output, code in outputs:
            docker = DockerProbe([output, 'ieum-zone-v1|actual|17|UTC'])
            with self.subTest(code=code, kind=type(output).__name__), self.assertRaises(zone.LegacyProbeError) as failure:
                zone.validate_legacy_zone(snapshot(), 'UTC', CID, docker, probe_attempts=3)
            self.assertEqual((failure.exception.code, failure.exception.attempts), (code, 1))
            self.assertEqual(len([call for call in docker.calls if call[0][0] == 'exec']), 1)
            self.assertNotIn(SECRET, str(failure.exception))

    def test_malformed_result_after_transient_failure_stops_without_last_retry(self):
        docker = DockerProbe([TimeoutError(SECRET), 'bad ' + SECRET, 'ieum-zone-v1|actual|17|UTC'])
        with self.assertRaises(zone.LegacyProbeError) as failure:
            zone.validate_legacy_zone(snapshot(), 'UTC', CID, docker, probe_attempts=3)
        self.assertEqual((failure.exception.code, failure.exception.attempts), ('probe_invalid_result', 2))
        self.assertEqual(len([call for call in docker.calls if call[0][0] == 'exec']), 2)

    def test_restart_during_retry_still_fails_identity_fence(self):
        changed = identity(); changed['startedAt'] = '2026-10-21T00:00:00Z'
        docker = DockerProbe([TimeoutError(SECRET), 'ieum-zone-v1|actual|17|UTC'],
                              identities=[identity(), changed])
        with self.assertRaisesRegex(RuntimeError, 'restarted or changed'):
            zone.validate_legacy_zone(snapshot(), 'UTC', CID, docker)

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
        result = reviewed_zone(snapshot(), 'UTC', CID, DockerProbe(output))
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
        result = reviewed_zone(snapshot({'TZ': 'UTC'}), 'Asia/Seoul', CID, DockerProbe(output))
        self.assertEqual(result['proof']['startupInputs']['basis'], 'jvm-startup-properties')
        self.assertEqual(result['proof']['startupInputs']['zones'], ['Asia/Seoul'])
        with self.assertRaisesRegex(RuntimeError, 'startup evidence differs'):
            zone.validate_legacy_zone(snapshot(), 'UTC', CID, DockerProbe(output))

    def test_process_tz_precedes_os_and_config_env_and_ignored_java_opts(self):
        output = startup_probe('ieum-zone-v2|input|TZ|zone|Asia/Seoul',
                               'ieum-zone-v2|input|etc-timezone|zone|UTC')
        environment = {'TZ': 'UTC', 'JAVA_OPTS': '-Duser.timezone=UTC'}
        result = reviewed_zone(snapshot(environment), 'Asia/Seoul', CID, DockerProbe(output))
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
            result = reviewed_zone(snapshot(), 'Etc/GMT-9', CID, DockerProbe(output))
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
        result = reviewed_zone(snapshot(), 'UTC', None, DockerProbe(output))
        self.assertEqual(result['origin'], 'running-jvm')

    def test_tzif_hash_supports_full_definition_match_without_guessing_offset(self):
        digest = zone.hashlib.sha256(zone._zone_bytes('UTC')).hexdigest()
        output = startup_probe('ieum-zone-v2|localtime-sha256|' + digest)
        result = reviewed_zone(snapshot(), 'UTC', CID, DockerProbe(output))
        self.assertTrue(result['proof']['startupInputs']['requestedTzifDigestMatches'])
        self.assertEqual(result['proof']['startupInputs']['status'], 'matches')
        result = reviewed_zone(snapshot(), 'Asia/Seoul', CID, DockerProbe(output))
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

    def test_regular_localtime_resolver_uses_container_bytes_and_deterministic_allowlist(self):
        container_bytes = zone._zone_bytes('Asia/Seoul') + b'container-tzdb-revision'
        result = self.resolver(container_bytes, {'UTC': zone._zone_bytes('UTC'),
                                                 'Asia/Seoul': container_bytes, 'ROK': container_bytes})
        self.assertEqual((result.returncode, result.stdout.strip(), result.stderr), (0, 'Asia/Seoul', ''))
        self.assertNotEqual(zone.hashlib.sha256(container_bytes).hexdigest(),
                            zone.hashlib.sha256(zone._zone_bytes('Asia/Seoul')).hexdigest())

    def test_regular_localtime_resolution_refuses_oversize_non_tzif_and_unmatched_data(self):
        for payload in (b'TZif' + b'x' * 1048573, b'not-a-zone-' + SECRET.encode(),
                        zone._zone_bytes('Asia/Seoul')):
            result = self.resolver(payload, {'UTC': zone._zone_bytes('UTC')})
            with self.subTest(size=len(payload)):
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, '')
                self.assertNotIn(SECRET, result.stdout + result.stderr)

    def test_resolver_never_uses_unbounded_cmp_when_n_option_is_unsupported(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / 'cmp'
            binary.write_text('#!/bin/sh\nprintf "private-cmp-diagnostic" >&2\nexit 2\n')
            binary.chmod(0o700)
            environment = dict(os.environ, PATH=directory + os.pathsep + os.environ.get('PATH', ''))
            result = self.resolver(zone._zone_bytes('UTC'), {'UTC': zone._zone_bytes('UTC')}, environment=environment)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout + result.stderr, '')
        self.assertIn('cmp -s -n 1048577', zone.LOCALTIME_RESOLVER_SCRIPT)
        self.assertIn('timeout 3s sh -c', zone.PROBE_SCRIPT)

    def test_regular_localtime_shell_probe_emits_resolved_container_zone_without_binary_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); process = root / 'proc/17'; process.mkdir(parents=True)
            (process / 'comm').write_text('java\n')
            (process / 'stat').write_text('17 (java) S ' + ('0 ' * 18) + '123\n')
            (process / 'environ').write_bytes(('SECRET=' + SECRET + '\0').encode())
            (process / 'cmdline').write_bytes(b'java\0-jar\0application.jar\0')
            (root / 'localtime').write_bytes(zone._zone_bytes('Asia/Seoul'))
            (root / 'zoneinfo/Asia').mkdir(parents=True)
            (root / 'zoneinfo/Asia/Seoul').write_bytes(zone._zone_bytes('Asia/Seoul'))
            script = zone.PROBE_SCRIPT.replace('/proc/', str(root / 'proc') + '/')
            script = script.replace('/etc/timezone', str(root / 'timezone')).replace('/etc/localtime', str(root / 'localtime'))
            script = script.replace('/usr/share/zoneinfo', str(root / 'zoneinfo'))
            script = script.replace('if command -v jcmd >/dev/null 2>&1 && command -v timeout >/dev/null 2>&1; then', 'if false; then')
            # macOS has no timeout utility; the finite synthetic fixture is bounded
            # by subprocess's deadline. Production retains its inner three seconds.
            script = script.replace('command -v timeout >/dev/null 2>&1', 'true').replace('timeout 3s sh -c', 'sh -c')
            result = subprocess.run(['sh', '-c', script, 'ieum-test', 'UTC Asia/Seoul'],
                                    check=True, capture_output=True, text=True, timeout=5)
            self.assertIn('ieum-zone-v2|input|etc-localtime|zone|Asia/Seoul', result.stdout)
            self.assertIn('ieum-zone-v2|localtime-resolution|resolved', result.stdout)
            self.assertNotIn(SECRET, result.stdout + result.stderr)
            self.assertNotIn('TZif', result.stdout + result.stderr)
            accepted = reviewed_zone(snapshot(), 'Asia/Seoul', CID, DockerProbe(result.stdout))
            self.assertEqual(accepted['proof']['startupInputs']['localtimeResolution'], 'resolved')
            with self.assertRaisesRegex(RuntimeError, 'startup evidence differs'):
                zone.validate_legacy_zone(snapshot(), 'UTC', CID, DockerProbe(result.stdout))
            (root / 'timezone').write_text('UTC\n')
            conflicting = subprocess.run(['sh', '-c', script, 'ieum-test', 'UTC Asia/Seoul'],
                                        check=True, capture_output=True, text=True, timeout=5)
            for requested in ('UTC', 'Asia/Seoul'):
                with self.subTest(requested=requested), self.assertRaisesRegex(RuntimeError, 'startup evidence differs'):
                    zone.validate_legacy_zone(snapshot(), requested, CID, DockerProbe(conflicting.stdout))
            unavailable = script.replace('&& true; then', '&& false; then')
            failed = subprocess.run(['sh', '-c', unavailable, 'ieum-test', 'UTC Asia/Seoul'],
                                    check=True, capture_output=True, text=True, timeout=5)
            self.assertIn('ieum-zone-v2|localtime-resolution|unresolved', failed.stdout)
            with self.assertRaisesRegex(RuntimeError, 'could not be resolved'):
                zone.validate_legacy_zone(snapshot(), 'Asia/Seoul', CID, DockerProbe(failed.stdout))

    def test_resolved_regular_localtime_conflicts_with_timezone_for_both_requested_zones(self):
        for read in ('ok', 'unavailable'):
            output = startup_probe('ieum-zone-v2|input|etc-timezone|zone|UTC',
                                   'ieum-zone-v2|input|etc-localtime|zone|Asia/Seoul',
                                   localtime_resolution='resolved', environment_read=read)
            for requested in ('UTC', 'Asia/Seoul'):
                with self.subTest(read=read, requested=requested), self.assertRaisesRegex(RuntimeError, 'startup evidence differs'):
                    zone.validate_legacy_zone(snapshot(), requested, CID, DockerProbe(output))

    def test_regular_localtime_without_timezone_allows_only_matching_reviewed_zone(self):
        output = startup_probe('ieum-zone-v2|input|etc-localtime|zone|Asia/Seoul', localtime_resolution='resolved')
        result = reviewed_zone(snapshot(), 'Asia/Seoul', CID, DockerProbe(output))
        self.assertEqual(result['proof']['startupInputs']['status'], 'matches')
        with self.assertRaisesRegex(RuntimeError, 'startup evidence differs'):
            zone.validate_legacy_zone(snapshot(), 'UTC', CID, DockerProbe(output))

    def test_unresolved_localtime_never_becomes_missing_or_an_operator_override(self):
        output = startup_probe('ieum-zone-v2|input|etc-localtime|unsupported', localtime_resolution='unresolved')
        for read in ('ok', 'unavailable'):
            probe = output.replace('|read|environment|ok', '|read|environment|' + read)
            with self.subTest(read=read), self.assertRaisesRegex(RuntimeError, 'could not be resolved'):
                zone.validate_legacy_zone(snapshot({'TZ': 'UTC'}), 'UTC', CID, DockerProbe(probe),
                                          confirmation_evidence='f' * 64)

    def test_complete_explicit_process_override_or_actual_jcmd_precedes_unresolved_platform(self):
        for source in ('TZ', 'cmdline'):
            output = startup_probe(f'ieum-zone-v2|input|{source}|zone|UTC',
                                   'ieum-zone-v2|input|etc-localtime|unsupported', localtime_resolution='unresolved')
            result = reviewed_zone(snapshot(), 'UTC', CID, DockerProbe(output))
            self.assertEqual(result['proof']['startupInputs']['localtimeResolution'], 'unresolved')
            partial = output.replace('|read|cmdline|ok', '|read|cmdline|unavailable')
            with self.assertRaisesRegex(RuntimeError, 'could not be resolved'):
                zone.validate_legacy_zone(snapshot(), 'UTC', CID, DockerProbe(partial))
        actual = startup_probe('ieum-zone-v2|input|etc-localtime|unsupported', localtime_resolution='unresolved',
                               actual='ieum-zone-v1|actual|17|UTC')
        self.assertEqual(zone.validate_legacy_zone(snapshot(), 'UTC', None, DockerProbe(actual))['origin'], 'running-jvm')

    def test_complete_explicit_override_precedes_platform_conflict_but_partial_cannot(self):
        output = startup_probe('ieum-zone-v2|input|cmdline|zone|UTC',
                               'ieum-zone-v2|input|etc-timezone|zone|UTC',
                               'ieum-zone-v2|input|etc-localtime|zone|Asia/Seoul', localtime_resolution='resolved')
        result = reviewed_zone(snapshot(), 'UTC', CID, DockerProbe(output))
        self.assertTrue(result['proof']['startupInputs']['platformInputsConflict'])
        with self.assertRaisesRegex(RuntimeError, 'startup evidence differs'):
            zone.validate_legacy_zone(snapshot(), 'UTC', CID,
                                      DockerProbe(output.replace('|read|environment|ok', '|read|environment|unavailable')))

    def test_resolution_record_is_required_unique_and_consistent_with_platform_input(self):
        valid = startup_probe()
        cases = [valid.replace('ieum-zone-v2|localtime-resolution|missing\n', ''),
                 valid.replace('|localtime-resolution|missing', '|localtime-resolution|resolved'),
                 valid.replace('|localtime-resolution|missing', '|localtime-resolution|' + SECRET),
                 valid.replace('ieum-zone-v2|complete', 'ieum-zone-v2|localtime-resolution|missing\nieum-zone-v2|complete')]
        for output in cases:
            with self.subTest(length=len(output)), self.assertRaises(zone.LegacyProbeError) as failure:
                zone.validate_legacy_zone(snapshot(), 'UTC', CID, DockerProbe(output), confirmation_evidence='f' * 64)
            self.assertEqual(failure.exception.attempts, 1)

    def test_cid_alone_exposes_sanitized_summary_and_requires_both_matching_confirmations(self):
        output = startup_probe('ieum-zone-v2|input|cmdline|zone|UTC', environment_read='unavailable')
        source = snapshot({'PASSWORD': SECRET})
        challenge = self.confirmation(source, 'Asia/Seoul', output)
        summary = challenge.summary
        self.assertEqual(challenge.code, 'legacy_zone_confirmation_required')
        self.assertEqual(summary['startupInputs']['status'], 'ambiguous')
        self.assertEqual(summary['startupInputs']['basis'], 'jvm-startup-properties')
        self.assertEqual(summary['startupInputs']['observedZones']['cmdline'], ['UTC'])
        self.assertFalse(summary['startupInputs']['processInputsComplete'])
        self.assertNotIn(SECRET, json.dumps(summary) + str(challenge))
        self.assertNotIn('PASSWORD', json.dumps(summary))
        expected = zone.hashlib.sha256(json.dumps(summary, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()
        self.assertEqual(challenge.fingerprint, expected)
        with self.assertRaises(zone.LegacyConfirmationRequired):
            zone.validate_legacy_zone(source, 'Asia/Seoul', None, DockerProbe(output), confirmation_evidence=expected)
        result = zone.validate_legacy_zone(source, 'Asia/Seoul', CID, DockerProbe(output), confirmation_evidence=expected)
        self.assertEqual(result['proof']['confirmationEvidence'], expected)
        self.assertEqual(result['proof']['confirmationSummary'], summary)

    def test_fingerprint_changes_when_request_or_sanitized_process_and_platform_evidence_changes(self):
        base = startup_probe('ieum-zone-v2|input|TZ|zone|UTC')
        fingerprint = self.confirmation(snapshot(), 'UTC', base).fingerprint
        cases = [(snapshot(), 'Etc/UTC', base),
                 (snapshot({'TZ': 'UTC'}), 'UTC', base),
                 (snapshot(), 'UTC', base.replace('|process|17|123', '|process|18|123')),
                 (snapshot(), 'UTC', base.replace('|process|17|123', '|process|17|124')),
                 (snapshot(), 'UTC', base.replace('|read|cmdline|ok', '|read|cmdline|unavailable')),
                 (snapshot(), 'UTC', startup_probe('ieum-zone-v2|input|TZ|zone|UTC',
                                                   'ieum-zone-v2|input|etc-timezone|zone|Asia/Seoul')),
                 (snapshot(), 'UTC', base.replace('|localtime-sha256|unavailable', '|localtime-sha256|' + 'c' * 64))]
        for source, requested, output in cases:
            with self.subTest(requested=requested, source=source.get('runtimeEnvironment')):
                challenge = self.confirmation(source, requested, output)
                self.assertNotEqual(challenge.fingerprint, fingerprint)
                with self.assertRaises(zone.LegacyConfirmationRequired):
                    zone.validate_legacy_zone(source, requested, CID, DockerProbe(output), confirmation_evidence=fingerprint)

    def test_fingerprint_binds_container_image_and_start_identities_without_exposing_strings(self):
        output = startup_probe()
        first = self.confirmation(snapshot(), 'UTC', output).fingerprint
        for key, value in (('containerId', 'c' * 64), ('image', 'sha256:' + 'd' * 64),
                           ('startedAt', '2026-10-21T00:00:00Z')):
            source = snapshot(); source[key] = value
            current = identity(); current.update(id=source['containerId'], image=source['image'], startedAt=source['startedAt'])
            docker = DockerProbe(output, identities=[current, current])
            with self.subTest(key=key), self.assertRaises(zone.LegacyConfirmationRequired) as failure:
                zone.validate_legacy_zone(source, 'UTC', source['containerId'], docker)
            self.assertNotEqual(failure.exception.fingerprint, first)

    def test_fingerprint_ignores_timeout_attempt_configuration_and_transient_retry_count(self):
        output = startup_probe('ieum-zone-v2|input|TZ|zone|UTC')
        first = self.confirmation(snapshot(), 'UTC', output, probe_timeout=6, probe_attempts=1)
        docker = DockerProbe([TimeoutError(SECRET), output])
        with self.assertRaises(zone.LegacyConfirmationRequired) as failure:
            zone.validate_legacy_zone(snapshot(), 'UTC', CID, docker, probe_timeout=60, probe_attempts=3)
        self.assertEqual(failure.exception.fingerprint, first.fingerprint)
        self.assertNotIn('probe', first.summary)

    def test_invalid_fingerprint_format_fails_before_any_docker_operation(self):
        for evidence in ('short', 'A' * 64, True, 1, SECRET):
            docker = DockerProbe()
            with self.subTest(evidence=evidence), self.assertRaisesRegex(RuntimeError, '64-character SHA-256'):
                zone.validate_legacy_zone(snapshot(), 'UTC', CID, docker, confirmation_evidence=evidence)
            self.assertFalse(docker.calls)

    def test_inspector_reports_actual_jvm_match_without_confirmation_or_raw_identity_strings(self):
        docker = DockerProbe()
        result = zone.inspect_legacy_zone(snapshot({'PASSWORD': SECRET}), 'UTC', docker)
        self.assertEqual(set(result), {'status', 'reason', 'evidence', 'evidenceSha256'})
        self.assertEqual((result['status'], result['reason']), ('running-jvm', 'observed_jvm_match'))
        evidence = result['evidence']
        self.assertEqual(evidence['observedZone'], 'UTC')
        self.assertEqual(evidence['pid'], 17)
        self.assertNotIn('probeUnavailableReason', evidence)
        self.assertNotIn('startupInputs', evidence)
        self.assertEqual(evidence['imageIdentitySha256'], zone.hashlib.sha256(IMAGE.encode()).hexdigest())
        encoded = json.dumps(result, sort_keys=True)
        self.assertNotIn(SECRET, encoded)
        self.assertNotIn('PASSWORD', encoded)
        self.assertNotIn(IMAGE, encoded)
        self.assertNotIn(snapshot()['startedAt'], encoded)
        self.assertEqual(result['evidenceSha256'], zone.hashlib.sha256(json.dumps(
            evidence, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest())
        self.assertEqual([args[0] for args, _ in docker.calls], ['inspect', 'exec', 'inspect'])

    def test_inspector_actual_jvm_carries_process_ticks_and_preserves_actual_precedence(self):
        output = startup_probe('ieum-zone-v2|input|etc-localtime|unsupported',
                               actual='ieum-zone-v1|actual|17|UTC')
        report = zone.inspect_legacy_zone(snapshot({'TZ': 'Asia/Seoul'}), 'UTC', DockerProbe(output))
        self.assertEqual(report['status'], 'running-jvm')
        self.assertEqual(report['evidence']['pidStartTicks'], '123')
        self.assertEqual(report['evidence']['configured']['status'], 'mismatch')
        self.assertNotIn('startupInputs', report['evidence'])

    def test_inspector_challenge_is_exactly_the_prepare_challenge_from_one_capture(self):
        output = startup_probe('ieum-zone-v2|input|cmdline|zone|UTC', environment_read='unavailable')
        source = snapshot({'PASSWORD': SECRET})
        docker = DockerProbe(output)
        report = zone.inspect_legacy_zone(source, 'Asia/Seoul', docker)
        challenge = self.confirmation(source, 'Asia/Seoul', output)
        self.assertEqual((report['status'], report['reason']),
                         ('confirmation-required', 'operator_confirmation_required'))
        self.assertEqual(report['evidence'], challenge.summary)
        self.assertEqual(report['evidenceSha256'], challenge.fingerprint)
        # Fixed pre-v11 preparation challenge for this fixture. Comparing both
        # current APIs alone would miss a shared accidental fingerprint change.
        self.assertEqual(report['evidenceSha256'],
                         '75f08d49679ed75bd6efc23032d95e031e055f2a9b93ec558492379e525b9af6')
        self.assertEqual(report['evidence']['startupInputs']['observedZones']['cmdline'], ['UTC'])
        self.assertEqual([args[0] for args, _ in docker.calls], ['inspect', 'exec', 'inspect'])
        approved = zone.validate_legacy_zone(source, 'Asia/Seoul', CID, DockerProbe(output),
                                             confirmation_evidence=report['evidenceSha256'])
        self.assertEqual(approved['origin'], 'operator-confirmed')
        self.assertEqual(approved['proof']['confirmationSummary'], report['evidence'])

    def test_inspector_returns_blocked_known_evidence_that_cannot_be_confirmed(self):
        conflict = startup_probe('ieum-zone-v2|input|etc-timezone|zone|UTC',
                                 'ieum-zone-v2|input|etc-localtime|zone|Asia/Seoul',
                                 localtime_resolution='resolved')
        cases = [
            (snapshot(), 'Asia/Seoul', 'ieum-zone-v1|actual|17|UTC', 'observed_jvm_mismatch'),
            (snapshot(), 'UTC', startup_probe('ieum-zone-v2|input|etc-localtime|unsupported'), 'localtime_unresolved'),
            (snapshot(), 'UTC', conflict, 'platform_conflict'),
            (snapshot(), 'Asia/Seoul', conflict, 'platform_conflict'),
            (snapshot(), 'UTC', startup_probe('ieum-zone-v2|input|TZ|zone|Asia/Seoul'), 'startup_mismatch'),
            (snapshot({'TZ': 'Asia/Seoul'}), 'UTC', 'ieum-zone-v1|unavailable|missing_tools', 'configuration_mismatch'),
            (snapshot({'TZ': 'Asia/Seoul'}), 'UTC', startup_probe(environment_read='unavailable'), 'configuration_mismatch'),
        ]
        for source, requested, output, reason in cases:
            with self.subTest(reason=reason, requested=requested):
                docker = DockerProbe(output)
                report = zone.inspect_legacy_zone(source, requested, docker)
                self.assertEqual((report['status'], report['reason']), ('blocked', reason))
                self.assertNotIn('error', report)
                self.assertNotIn('proof', report)
                self.assertEqual([args[0] for args, _ in docker.calls], ['inspect', 'exec', 'inspect'])
                with self.assertRaises(RuntimeError) as failure:
                    zone.validate_legacy_zone(source, requested, CID, DockerProbe(output),
                                              confirmation_evidence=report['evidenceSha256'])
                self.assertNotIsInstance(failure.exception, zone.LegacyConfirmationRequired)

    def test_inspector_unresolved_summary_has_explicit_resolution_and_no_missing_inference(self):
        output = startup_probe('ieum-zone-v2|input|etc-localtime|unsupported')
        report = zone.inspect_legacy_zone(snapshot({'PASSWORD': SECRET}), 'UTC', DockerProbe(output))
        startup = report['evidence']['startupInputs']
        self.assertEqual((startup['status'], startup['localtimeResolution']), ('unresolved', 'unresolved'))
        self.assertEqual(startup['sourceKinds']['etc-localtime'], ['unsupported'])
        self.assertNotIn(SECRET, json.dumps(report))
        self.assertNotIn('GMT', json.dumps(report))

    def test_inspector_distinguishes_higher_explicit_mismatch_from_overridden_platform_conflict(self):
        output = startup_probe('ieum-zone-v2|input|cmdline|zone|Asia/Seoul',
                               'ieum-zone-v2|input|etc-timezone|zone|UTC',
                               'ieum-zone-v2|input|etc-localtime|zone|Asia/Seoul',
                               localtime_resolution='resolved')
        report = zone.inspect_legacy_zone(snapshot(), 'UTC', DockerProbe(output))
        self.assertEqual((report['status'], report['reason']), ('blocked', 'startup_mismatch'))
        self.assertEqual(report['evidence']['startupInputs']['basis'], 'jvm-startup-properties')
        accepted = zone.inspect_legacy_zone(snapshot(), 'Asia/Seoul', DockerProbe(output))
        self.assertEqual(accepted['status'], 'confirmation-required')

    def test_inspector_does_not_approve_or_reuse_changed_observations(self):
        first = startup_probe('ieum-zone-v2|input|TZ|zone|UTC')
        report = zone.inspect_legacy_zone(snapshot(), 'UTC', DockerProbe(first))
        with self.assertRaises(zone.LegacyConfirmationRequired):
            zone.validate_legacy_zone(snapshot(), 'UTC', None, DockerProbe(first),
                                      confirmation_evidence=report['evidenceSha256'])
        changed = first.replace('|process|17|123', '|process|17|124')
        with self.assertRaises(zone.LegacyConfirmationRequired) as failure:
            zone.validate_legacy_zone(snapshot(), 'UTC', CID, DockerProbe(changed),
                                      confirmation_evidence=report['evidenceSha256'])
        self.assertNotEqual(failure.exception.fingerprint, report['evidenceSha256'])

    def test_inspector_never_returns_an_assessment_for_malformed_or_failed_probe(self):
        for output in (SECRET, '', startup_probe().replace('ieum-zone-v2|complete', ''),
                       RuntimeError(SECRET), TimeoutError(SECRET)):
            with self.subTest(output_type=type(output).__name__), self.assertRaises(zone.LegacyProbeError) as failure:
                zone.inspect_legacy_zone(snapshot(), 'UTC', DockerProbe(output), probe_attempts=1)
            self.assertNotIn(SECRET, str(failure.exception))
            self.assertEqual(failure.exception.attempts, 1)

    def test_inspector_keeps_identity_before_and_after_probe_and_probe_option_bounds(self):
        for identities in ([{**identity(), 'startedAt': 'changed'}],
                           [identity(), {**identity(), 'image': 'changed'}]):
            with self.subTest(identities=identities), self.assertRaises(RuntimeError):
                zone.inspect_legacy_zone(snapshot(), 'UTC', DockerProbe(identities=identities))
        for options in ({'probe_timeout': 5}, {'probe_timeout': True}, {'probe_attempts': 4}):
            docker = DockerProbe()
            with self.subTest(options=options), self.assertRaises(RuntimeError):
                zone.inspect_legacy_zone(snapshot(), 'UTC', docker, **options)
            self.assertFalse(docker.calls)

    def test_inspector_fingerprint_excludes_probe_attempts_and_timeout(self):
        output = startup_probe('ieum-zone-v2|input|TZ|zone|UTC')
        first = zone.inspect_legacy_zone(snapshot(), 'UTC', DockerProbe(output), probe_timeout=6, probe_attempts=1)
        retried = zone.inspect_legacy_zone(snapshot(), 'UTC', DockerProbe([TimeoutError(SECRET), output]),
                                           probe_timeout=60, probe_attempts=3)
        self.assertEqual(first, retried)


if __name__ == '__main__':
    unittest.main()
