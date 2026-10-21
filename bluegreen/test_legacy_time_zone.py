import importlib.util
import json
from pathlib import Path
import subprocess
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

    def test_probe_exception_and_unrecognized_output_are_sanitized_unavailable(self):
        for output in (RuntimeError(SECRET), 'user.password=' + SECRET, 'x' * 4097,
                       'ieum-zone-v1|actual|17|' + SECRET):
            with self.subTest(kind=type(output).__name__):
                result = zone.validate_legacy_zone(snapshot(), 'UTC', CID, DockerProbe(output))
                self.assertEqual(result['origin'], 'operator-confirmed')
                self.assertNotIn(SECRET, json.dumps(result))

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


if __name__ == '__main__':
    unittest.main()
