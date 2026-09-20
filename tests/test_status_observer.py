"""Host observations use a fake Docker boundary; publication uses real private files."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import status_io as io
import status_observer as observer

AT = '2026-09-20T12:00:00Z'
OLD = '2026-08-01T12:00:00Z'
SECRET = 'private://credential-token'


class Runner:
    def __init__(self):
        self.fail = set()
        self.unsupported = set()
        self.found = 'a' * 64
        self.config = {'name': 'selected', 'services': {'caddy': {
            'image': 'private/image:2.11.4@sha256:' + 'b' * 64, 'environment': {'SECRET': SECRET}}}}
        self.doc = {'Id': self.found, 'Image': 'sha256:' + 'c' * 64,
                    'Config': {'Labels': {'com.docker.compose.project': 'selected',
                               'com.docker.compose.service': 'caddy', 'com.docker.compose.oneoff': 'False'}},
                    'State': {'Status': 'running', 'Paused': False, 'Health': {'Status': 'healthy'}}}

    def __call__(self, args, **options):
        self.last = args
        if 'config' in args:
            kind, result = 'config', json.dumps(self.config)
        elif args[1] == 'ps':
            kind, result = 'inventory', self.found
        elif args[1] == 'inspect':
            kind, result = 'inspect', json.dumps([self.doc])
        elif args[-1] == 'version':
            kind, result = 'version', 'v2.11.4 h1:build'
        else:
            kind, result = 'health', ''
        if kind in self.unsupported:
            raise io.Unsupported()
        if kind in self.fail:
            raise io.Unavailable(SECRET)
        return result


class ObserverTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root, self.env, self.runner = Path(tmp.name), Path(tmp.name) / '.env', Runner()

    def collect(self):
        return observer.collect(self.root, self.env, self.runner, lambda: AT)

    def test_actual_probe_and_versions_are_distinct_from_configuration(self):
        doc = self.collect()
        row = doc['components'][0]
        self.assertEqual(row['state'], 'healthy')
        self.assertEqual(row['observedVersion'], '2.11.4')
        self.assertEqual(row['configuredVersion'], '2.11.4')
        self.assertNotEqual(row['configuredDigest'], row['observedImageId'])
        self.assertNotIn(SECRET, json.dumps(doc))
        self.runner.fail.add('health')
        self.assertEqual(self.collect()['components'][0]['state'], 'unavailable')
        self.runner.fail.add('version')
        self.assertNotIn('observedVersion', self.collect()['components'][0])

    def test_probe_that_cannot_run_is_unknown_not_unavailable(self):
        self.runner.unsupported.add('health')
        row = self.collect()['components'][0]
        self.assertEqual(row['state'], 'unknown')
        self.assertEqual(row['observedVersion'], '2.11.4')

    def test_empty_or_failed_inventory_does_not_claim_a_missing_installation(self):
        self.runner.found = ''
        self.assertEqual(self.collect()['components'][0]['state'], 'unknown')
        self.runner.fail.add('inventory')
        self.assertEqual(self.collect()['components'][0]['state'], 'unknown')
        self.runner.fail = {'config'}
        doc = self.collect()
        self.assertIsNone(doc['configurationObservedAt'])
        self.assertIsNone(doc['components'][0]['configured'])
        self.assertNotIn('configuredVersion', doc['components'][0])

    def test_wrong_identity_paused_and_stopped_are_not_healthy(self):
        for state, expected in [('exited', 'unavailable'), ('restarting', 'starting')]:
            self.runner.doc['State']['Status'] = state
            self.assertEqual(self.collect()['components'][0]['state'], expected)
        self.runner.doc['State'].update(Status='running', Paused=True)
        self.assertEqual(self.collect()['components'][0]['state'], 'unknown')
        self.runner.doc['Config']['Labels']['com.docker.compose.project'] = 'foreign'
        self.assertIsNone(self.collect()['components'][0]['observedAt'])

    def test_tasks_keep_execution_time_and_frozen_files_do_not_renew_it(self):
        io.task_record(self.root, self.env, OLD, 'healthy')
        doc = observer.observe(self.root, self.env, self.runner, lambda: AT)
        row = doc['components'][1]
        self.assertEqual((row['observedAt'], row['lastExecutionAt']), (AT, OLD))
        before = (self.root / 'data/console/status.json').read_bytes()
        self.assertEqual(json.loads(before), doc)
        self.assertEqual((self.root / 'data/console/status.json').read_bytes(), before)
        io.task_record(self.root, self.env, '2026-09-20T12:01:00Z', 'healthy')
        self.assertEqual(self.collect()['components'][1]['state'], 'unknown')

    def test_malformed_and_private_version_values_never_escape(self):
        self.runner.config['services']['caddy']['image'] = SECRET
        row = self.collect()['components'][0]
        self.assertNotIn('configuredVersion', row)
        for value in [SECRET, '1.2.3-private', '١.٢.٣', None]:
            self.assertIsNone(observer.version(value))
        self.runner.config['name'] = None
        self.assertIsNone(self.collect()['configurationObservedAt'])

    def test_publication_lock_collision_is_a_successful_no_op(self):
        with patch.object(observer.fcntl, 'flock', side_effect=BlockingIOError):
            self.assertIsNone(observer.observe(self.root, self.env, self.runner, lambda: AT))
        self.assertFalse((self.root / 'data/console/status.json').exists())

    def test_unrelated_publication_lock_storage_failure_is_not_suppressed(self):
        original = observer.os.open
        def fail_lock(path, *args, **kwargs):
            if path == '.status.lock':
                raise BlockingIOError()
            return original(path, *args, **kwargs)
        with patch.object(observer.os, 'open', side_effect=fail_lock):
            with self.assertRaises(BlockingIOError):
                observer.observe(self.root, self.env, self.runner, lambda: AT)


if __name__ == '__main__':
    unittest.main()
