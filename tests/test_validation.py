from copy import deepcopy
import unittest

from support import load

validation = load("validation")


class ValidationTests(unittest.TestCase):
    def test_only_private_unicast_ipv4_and_valid_credentials(self):
        validation.validate_device("192.168.10.40", 8000, "admin", "example-only")
        for host in ("127.0.0.1", "169.254.169.254", "8.8.8.8", "224.0.0.1", "http://device", "::1"):
            with self.assertRaises(validation.SetupError):
                validation.validate_device(host, 8000, "admin", "example-only")

    def test_invalid_ports_and_credentials(self):
        for port in (0, 65536, True, 3.5):
            with self.assertRaises(validation.SetupError):
                validation.validate_device("10.0.0.3", port, "admin", "example-only")
        for password in ("", "x" * 65, "secret\nvalue"):
            with self.assertRaises(validation.SetupError):
                validation.validate_device("10.0.0.3", 8000, "admin", password)

    def test_merge_preserves_other_devices_and_options(self):
        options = {"doorbells": [{"name": "Other", "ip": "10.0.0.4", "password": "fixture-only"}],
                   "mqtt": {"fixture": "preserve"}, "system": {"sdk_log_level": "NONE"}}
        original = deepcopy(options)
        merged, name = validation.merge_device(options,
            {"host": "10.0.0.5", "port": 8000, "username": "admin", "password": "example-only"}, None)
        self.assertEqual(options, original)
        self.assertEqual(merged["doorbells"][0], original["doorbells"][0])
        self.assertEqual(merged["mqtt"], original["mqtt"])
        self.assertEqual(merged["system"], original["system"])
        self.assertEqual(name, "chime_10_0_0_5")

    def test_existing_target_is_adopted_without_entity_rename(self):
        options = {"doorbells": [{"name": "My Panel", "ip": "10.0.0.5", "snapshot": True}]}
        merged, name = validation.merge_device(options,
            {"host": "10.0.0.5", "port": 8000, "username": "admin", "password": "example-only"}, None)
        self.assertEqual(name, "My Panel")
        self.assertEqual(len(merged["doorbells"]), 1)
        self.assertTrue(merged["doorbells"][0]["snapshot"])

    def test_ambiguous_target_and_colliding_normalized_names_fail(self):
        device = {"host": "10.0.0.5", "port": 8000, "username": "admin", "password": "example-only"}
        duplicate = {"doorbells": [{"name": "A", "ip": "10.0.0.5"}, {"name": "B", "ip": "10.0.0.5"}]}
        with self.assertRaises(validation.SetupError): validation.merge_device(duplicate, device, None)
        collision = {"doorbells": [{"name": "My Panel", "ip": "10.0.0.5"}, {"name": "MyPanel", "ip": "10.0.0.6"}]}
        with self.assertRaises(validation.SetupError): validation.merge_device(collision, device, None)

    def test_reconfigure_does_not_silently_append_missing_target(self):
        with self.assertRaisesRegex(validation.SetupError, "bridge_device_missing"):
            validation.merge_device({"doorbells": []},
                {"host": "10.0.0.5", "port": 8000, "username": "admin", "password": "example-only"}, "Gone")

    def test_reconfigure_cannot_duplicate_another_device_address(self):
        options = {"doorbells": [{"name": "A", "ip": "10.0.0.4"}, {"name": "B", "ip": "10.0.0.5"}]}
        with self.assertRaises(validation.SetupError):
            validation.merge_device(options, {"host": "10.0.0.5", "port": 8000,
                "username": "admin", "password": "example-only"}, "A")

    def test_user_partial_device_is_not_discarded(self):
        planned = {"name": "Planned", "ip": "", "password": "", "snapshot": True}
        merged, _ = validation.merge_device({"doorbells": [planned]},
            {"host": "10.0.0.5", "port": 8000, "username": "admin", "password": "example-only"}, None)
        self.assertEqual(merged["doorbells"][0], planned)
