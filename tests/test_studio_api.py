import unittest
from unittest.mock import patch

from robonix_client import app as client_app


class StudioDiscoveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_discover_studio_services(self):
        def fake_probe(host, port, timeout=0.35):
            if port == 50107:
                return {"port": 50107, "title": "scene — semantic map", "status": 200, "online": True}
            if port == 8091:
                return {"port": 8091, "title": "Robonix · mapping", "status": 200, "online": True}
            return None

        with patch("robonix_client.app._probe_http_port_sync", side_effect=fake_probe):
            res = await client_app.discover_studio_services(robot_host="127.0.0.1")
            self.assertTrue(res.get("ok"))
            services = res.get("services", [])
            self.assertEqual(len(services), 2)
            self.assertEqual(services[0]["port"], 50107)
            self.assertEqual(services[0]["name"], "Scene Service")
            self.assertEqual(services[1]["port"], 8091)
            self.assertEqual(services[1]["name"], "SLAM Mapping")

    async def test_discover_studio_services_with_extra_ports(self):
        def fake_probe(host, port, timeout=0.35):
            if port == 9999:
                return {"port": 9999, "title": "Custom Tool", "status": 200, "online": True}
            return None

        with patch("robonix_client.app._probe_http_port_sync", side_effect=fake_probe):
            res = await client_app.discover_studio_services(robot_host="127.0.0.1", extra_ports="9999,invalid")
            self.assertTrue(res.get("ok"))
            services = res.get("services", [])
            self.assertEqual(len(services), 1)
            self.assertEqual(services[0]["port"], 9999)
            self.assertEqual(services[0]["name"], "Custom Tool")


if __name__ == "__main__":
    unittest.main()
