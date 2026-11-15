"""Ownership and expiry contracts for the deployment maintenance heartbeat."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]


class MaintenanceLeaseContracts(unittest.TestCase):
    def setUp(self):
        path = HERE / 'maintenance_lease.py'
        self.assertTrue(path.is_file(), 'bounded owner-specific maintenance leases are required')
        spec = importlib.util.spec_from_file_location('maintenance_lease', path)
        self.api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.api)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state = Path(self.temporary.name)
        self.parent = os.getpid()

    def test_fresh_owner_is_exclusive_and_old_owner_cannot_renew_or_remove_a_replacement(self):
        owner = self.api.acquire(self.state, self.parent)
        self.api.check(self.state, owner)
        with self.assertRaises(RuntimeError):
            self.api.acquire(self.state, self.parent)
        marker = self.state / 'maintenance'
        os.utime(marker, (0, 0))
        replacement = self.api.acquire(self.state, self.parent)
        self.assertNotEqual(owner, replacement)
        with self.assertRaises(RuntimeError):
            self.api.renew(self.state, owner)
        self.assertFalse(self.api.release(self.state, owner))
        self.assertEqual(json.loads(marker.read_text())['owner'], replacement)
        self.assertTrue(self.api.release(self.state, replacement))
        self.assertFalse(marker.exists())

    def test_total_lease_lifetime_is_bounded_even_while_the_marker_is_fresh(self):
        with patch.object(self.api.time, 'monotonic', return_value=100):
            owner = self.api.acquire(self.state, self.parent)
        marker = self.state / 'maintenance'
        before = marker.stat().st_mtime_ns
        with patch.object(self.api.time, 'monotonic', return_value=100 + self.api.MAX_LIFETIME_SECONDS + 1):
            with self.assertRaises(RuntimeError):
                self.api.renew(self.state, owner)
            with self.assertRaises(RuntimeError):
                self.api.check(self.state, owner)
        self.assertEqual(marker.stat().st_mtime_ns, before)

    def test_heartbeat_renews_only_its_live_parent_and_stops_after_parent_exit(self):
        owner = self.api.acquire(self.state, self.parent)
        marker = self.state / 'maintenance'
        refreshed = []
        def pause(_seconds):
            refreshed.append(marker.stat().st_mtime)
            old = self.api.time.time() - 60
            os.utime(marker, (old, old))
        with patch.object(self.api.os, 'getppid', side_effect=[self.parent, self.parent, self.parent + 1]), \
                patch.object(self.api.time, 'sleep', side_effect=pause):
            self.api.keepalive(self.state, owner, self.parent)
        self.assertEqual(len(refreshed), 2)
        self.assertGreater(refreshed[1], marker.stat().st_mtime + 55)


if __name__ == '__main__':
    unittest.main()
