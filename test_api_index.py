import unittest
import json
import io
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.index import application, handler


class TestApiIndex(unittest.TestCase):
    def _call_wsgi(self, path="/", method="GET", body=None):
        environ = {
            "PATH_INFO": path,
            "REQUEST_METHOD": method,
            "wsgi.input": io.BytesIO(body.encode("utf-8") if body else b""),
            "CONTENT_LENGTH": str(len(body.encode("utf-8"))) if body else "0"
        }
        status_holder = {}
        headers_holder = {}

        def start_response(status, headers):
            status_holder["status"] = status
            headers_holder["headers"] = dict(headers)

        resp_chunks = application(environ, start_response)
        resp_data = b"".join(resp_chunks).decode("utf-8")
        return status_holder.get("status"), headers_holder.get("headers"), json.loads(resp_data)

    def test_health_routes(self):
        for path in ["/", "/health", "/api/health"]:
            status, headers, data = self._call_wsgi(path=path)
            self.assertTrue(status.startswith("200"))
            self.assertEqual(data.get("status"), "online")
            self.assertEqual(data.get("service"), "driver-drowsiness-ai")
            self.assertEqual(headers.get("Content-Type"), "application/json")

    def test_status_endpoint(self):
        status, headers, data = self._call_wsgi(path="/api/status")
        self.assertTrue(status.startswith("200"))
        self.assertEqual(data.get("status"), "ready")
        self.assertIn("thresholds", data)
        self.assertIn("ear_closed_threshold", data["thresholds"])

    def test_predictions_get_and_post(self):
        # Initial GET
        status, _, data = self._call_wsgi(path="/api/predictions", method="GET")
        self.assertTrue(status.startswith("200"))
        self.assertIn("predictions", data)

        # POST new telemetry reading
        payload = json.dumps({
            "ear": 0.18,
            "perclos": 0.45,
            "mar": 0.70,
            "head_pitch": 12.5,
            "head_yaw": -5.0,
            "alert_state": "warning"
        })
        post_status, _, post_data = self._call_wsgi(path="/api/predictions", method="POST", body=payload)
        self.assertTrue(post_status.startswith("201"))
        self.assertEqual(post_data.get("status"), "received")
        self.assertEqual(post_data["record"]["alert_state"], "warning")

        # GET again should show the posted record
        _, _, updated = self._call_wsgi(path="/api/predictions", method="GET")
        self.assertGreater(len(updated["predictions"]), 0)
        self.assertEqual(updated["predictions"][0]["alert_state"], "warning")

    def test_alerts_get_and_post(self):
        alert_payload = json.dumps({
            "level": "critical",
            "channel": "all",
            "message": "Microsleep emergency triggered"
        })
        status, _, data = self._call_wsgi(path="/api/alerts", method="POST", body=alert_payload)
        self.assertTrue(status.startswith("201"))
        self.assertEqual(data.get("status"), "alert_logged")

        # GET alerts
        status, _, get_data = self._call_wsgi(path="/api/alerts", method="GET")
        self.assertTrue(status.startswith("200"))
        self.assertGreater(len(get_data["alerts"]), 0)

    def test_csv_export(self):
        payload = json.dumps({"ear": 0.2, "perclos": 0.5, "mar": 0.3, "alert_state": "warning"})
        self._call_wsgi(path="/api/predictions", method="POST", body=payload)
        environ = {"PATH_INFO": "/api/predictions", "REQUEST_METHOD": "GET",
                   "QUERY_STRING": "format=csv", "wsgi.input": io.BytesIO(b""), "CONTENT_LENGTH": "0"}
        seen = {}
        def start_response(status, headers):
            seen["status"] = status
            seen["headers"] = dict(headers)
        body = b"".join(application(environ, start_response)).decode("utf-8")
        self.assertTrue(seen["status"].startswith("200"))
        self.assertTrue(seen["headers"]["Content-Type"].startswith("text/csv"))
        self.assertIn("attachment", seen["headers"]["Content-Disposition"])
        lines = body.strip().splitlines()
        self.assertEqual(lines[0], "timestamp,ear,mar,perclos,head_pitch,head_yaw,alert_state,model_version")
        self.assertGreaterEqual(len(lines), 2)

    def test_not_found(self):
        status, _, data = self._call_wsgi(path="/invalid-route")
        self.assertTrue(status.startswith("404"))
        self.assertIn("error", data)


if __name__ == "__main__":
    unittest.main()
