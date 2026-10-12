"""
Cloud Serverless API Entrypoint (Vercel, AWS Lambda, Render, WSGI)
Provides a lightweight, zero-external-dependency RESTful API service for:
- System health diagnostic probes
- Runtime configuration & threshold discovery
- Live telemetry ingestion and query (bridging local edge cameras to cloud dashboard)
- Real-time alert notifications and logging
"""

import json
import time
import os
import sqlite3
from urllib.parse import parse_qs

# In-memory circular buffer for telemetry predictions and alerts
# Ensures instant, sub-millisecond response in serverless environments without requiring persistent disk
MAX_HISTORY = 50
_predictions_buffer = []
_alerts_buffer = []

# Default Edge-AI System Thresholds
SYSTEM_THRESHOLDS = {
    "ear_closed_threshold": 0.21,
    "mar_yawn_threshold": 0.65,
    "head_pitch_nod_threshold": 18.0,
    "head_yaw_distraction_threshold": 25.0,
    "perclos_window_seconds": 30,
    "perclos_watch_threshold": 0.25,
    "perclos_warning_threshold": 0.40,
    "perclos_critical_threshold": 0.60,
    "watch_consecutive_seconds": 3.0,
    "warning_consecutive_seconds": 5.0,
    "critical_consecutive_seconds": 10.0,
    "alert_cooldown_seconds": 10.0
}


def _get_sqlite_conn():
    """Optional ephemeral SQLite connection for serverless /tmp or local runs."""
    db_path = os.environ.get("DATABASE_PATH", "/tmp/drowsiness.db" if os.name != "nt" else "data/logs/drowsiness.db")
    try:
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = sqlite3.connect(db_path, timeout=1.0)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                perclos REAL,
                ear REAL,
                mar REAL,
                head_pitch REAL,
                head_yaw REAL,
                alert_state TEXT,
                model_version TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                level TEXT,
                channel TEXT,
                message TEXT
            )
        """)
        return conn
    except Exception:
        return None


def _csv_response(start_response, headers, filename, columns, rows):
    """Return rows (list of dicts) as a downloadable CSV."""
    import csv
    import io
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    body = buf.getvalue().encode("utf-8")
    csv_headers = [h for h in headers if h[0] != "Content-Type"] + [
        ("Content-Type", "text/csv; charset=utf-8"),
        ("Content-Disposition", f'attachment; filename="{filename}"'),
        ("Content-Length", str(len(body))),
    ]
    start_response("200 OK", csv_headers)
    return [body]


def application(environ, start_response):
    """
    Standard WSGI application entrypoint compatible with Vercel Python runtime,
    Gunicorn, uWSGI, and local wsgiref servers.
    """
    global _predictions_buffer, _alerts_buffer

    path = environ.get("PATH_INFO", "/")
    method = environ.get("REQUEST_METHOD", "GET").upper()

    cors_headers = [
        ("Content-Type", "application/json"),
        ("Access-Control-Allow-Origin", "*"),
        ("Access-Control-Allow-Methods", "GET, POST, OPTIONS"),
        ("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Requested-With"),
        ("Cache-Control", "no-cache, no-store, must-revalidate")
    ]

    # Handle CORS preflight
    if method == "OPTIONS":
        start_response("204 No Content", cors_headers)
        return [b""]

    # 1. Health check & root discovery endpoints
    if path in ("/", "/health", "/api", "/api/health"):
        data = {
            "status": "online",
            "service": "driver-drowsiness-ai",
            "version": "1.0.0",
            "message": "Edge-AI Driver Drowsiness Detection API is healthy",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "endpoints": {
                "/health": "Service health probe",
                "/api/status": "System threshold configurations",
                "/api/predictions": "Latest telemetry ingestion and history",
                "/api/alerts": "Recent alert notifications"
            }
        }
        body = json.dumps(data, indent=2).encode("utf-8")
        start_response("200 OK", cors_headers + [("Content-Length", str(len(body)))])
        return [body]

    # 2. System Status & Thresholds
    elif path == "/api/status":
        data = {
            "status": "ready",
            "service": "driver-drowsiness-ai",
            "thresholds": SYSTEM_THRESHOLDS,
            "buffer_count": len(_predictions_buffer)
        }
        body = json.dumps(data, indent=2).encode("utf-8")
        start_response("200 OK", cors_headers + [("Content-Length", str(len(body)))])
        return [body]

    # 3. Telemetry Predictions (GET query, POST ingest)
    elif path in ("/api/predictions", "/api/telemetry"):
        if method == "GET":
            # Return recent predictions from in-memory ring buffer
            # If buffer is empty, try querying SQLite or return baseline default
            preds = list(reversed(_predictions_buffer[-20:]))
            if not preds:
                conn = _get_sqlite_conn()
                if conn:
                    try:
                        cursor = conn.cursor()
                        cursor.execute("SELECT timestamp, perclos, ear, mar, head_pitch, head_yaw, alert_state, model_version FROM predictions ORDER BY id DESC LIMIT 20")
                        for row in cursor.fetchall():
                            preds.append({
                                "timestamp": row[0],
                                "perclos": row[1],
                                "ear": row[2],
                                "mar": row[3],
                                "head_pitch": row[4],
                                "head_yaw": row[5],
                                "alert_state": row[6],
                                "model_version": row[7]
                            })
                        conn.close()
                    except Exception:
                        pass

            if parse_qs(environ.get("QUERY_STRING", "")).get("format", [""])[0] == "csv":
                return _csv_response(
                    start_response, cors_headers, "drowsiness_predictions.csv",
                    ["timestamp", "ear", "mar", "perclos", "head_pitch", "head_yaw", "alert_state", "model_version"],
                    preds)

            data = {
                "status": "ok",
                "count": len(preds),
                "predictions": preds
            }
            body = json.dumps(data, indent=2).encode("utf-8")
            start_response("200 OK", cors_headers + [("Content-Length", str(len(body)))])
            return [body]

        elif method == "POST":
            # Ingest live telemetry reading from local camera detector or client
            try:
                content_length = int(environ.get("CONTENT_LENGTH", 0))
                req_body = environ["wsgi.input"].read(content_length).decode("utf-8")
                payload = json.loads(req_body) if req_body else {}
            except Exception as e:
                err = json.dumps({"error": f"Invalid JSON payload: {str(e)}"}).encode("utf-8")
                start_response("400 Bad Request", cors_headers + [("Content-Length", str(len(err)))])
                return [err]

            record = {
                "timestamp": payload.get("timestamp", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
                "ear": round(float(payload.get("ear", 0.28)), 4),
                "perclos": round(float(payload.get("perclos", 0.12)), 4),
                "mar": round(float(payload.get("mar", 0.25)), 4),
                "head_pitch": round(float(payload.get("head_pitch", 0.0)), 2),
                "head_yaw": round(float(payload.get("head_yaw", 0.0)), 2),
                "alert_state": str(payload.get("alert_state", "normal")).lower(),
                "model_version": str(payload.get("model_version", "cloud_v1"))
            }

            _predictions_buffer.append(record)
            if len(_predictions_buffer) > MAX_HISTORY:
                _predictions_buffer.pop(0)

            # Persist to SQLite if available
            conn = _get_sqlite_conn()
            if conn:
                try:
                    conn.execute(
                        "INSERT INTO predictions (timestamp, perclos, ear, mar, head_pitch, head_yaw, alert_state, model_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (record["timestamp"], record["perclos"], record["ear"], record["mar"], record["head_pitch"], record["head_yaw"], record["alert_state"], record["model_version"])
                    )
                    conn.commit()
                    conn.close()
                except Exception:
                    pass

            resp = json.dumps({"status": "received", "record": record}).encode("utf-8")
            start_response("201 Created", cors_headers + [("Content-Length", str(len(resp)))])
            return [resp]

    # 4. Alert Notifications (GET recent, POST trigger)
    elif path == "/api/alerts":
        if method == "GET":
            alerts = list(reversed(_alerts_buffer[-20:]))
            if not alerts:
                conn = _get_sqlite_conn()
                if conn:
                    try:
                        cursor = conn.cursor()
                        cursor.execute("SELECT timestamp, level, channel, message FROM alerts ORDER BY id DESC LIMIT 20")
                        for row in cursor.fetchall():
                            alerts.append({
                                "timestamp": row[0],
                                "level": row[1],
                                "channel": row[2],
                                "message": row[3]
                            })
                        conn.close()
                    except Exception:
                        pass

            if parse_qs(environ.get("QUERY_STRING", "")).get("format", [""])[0] == "csv":
                return _csv_response(
                    start_response, cors_headers, "drowsiness_alerts.csv",
                    ["timestamp", "level", "channel", "message"],
                    alerts)

            data = {
                "status": "ok",
                "count": len(alerts),
                "alerts": alerts
            }
            body = json.dumps(data, indent=2).encode("utf-8")
            start_response("200 OK", cors_headers + [("Content-Length", str(len(body)))])
            return [body]

        elif method == "POST":
            try:
                content_length = int(environ.get("CONTENT_LENGTH", 0))
                req_body = environ["wsgi.input"].read(content_length).decode("utf-8")
                payload = json.loads(req_body) if req_body else {}
            except Exception as e:
                err = json.dumps({"error": f"Invalid JSON payload: {str(e)}"}).encode("utf-8")
                start_response("400 Bad Request", cors_headers + [("Content-Length", str(len(err)))])
                return [err]

            alert_record = {
                "timestamp": payload.get("timestamp", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
                "level": str(payload.get("level", "watch")),
                "channel": str(payload.get("channel", "visual")),
                "message": str(payload.get("message", "Driver fatigue alert triggered"))
            }

            _alerts_buffer.append(alert_record)
            if len(_alerts_buffer) > MAX_HISTORY:
                _alerts_buffer.pop(0)

            conn = _get_sqlite_conn()
            if conn:
                try:
                    conn.execute(
                        "INSERT INTO alerts (timestamp, level, channel, message) VALUES (?, ?, ?, ?)",
                        (alert_record["timestamp"], alert_record["level"], alert_record["channel"], alert_record["message"])
                    )
                    conn.commit()
                    conn.close()
                except Exception:
                    pass

            resp = json.dumps({"status": "alert_logged", "alert": alert_record}).encode("utf-8")
            start_response("201 Created", cors_headers + [("Content-Length", str(len(resp)))])
            return [resp]

    # Route Not Found (404)
    data = {
        "error": "Route not found",
        "path": path,
        "method": method,
        "valid_endpoints": ["/health", "/api/status", "/api/predictions", "/api/alerts"]
    }
    body = json.dumps(data, indent=2).encode("utf-8")
    start_response("404 Not Found", cors_headers + [("Content-Length", str(len(body)))])
    return [body]


# Top-level callables for Vercel, WSGI, and ASGI wrappers
app = application
handler = application


if __name__ == "__main__":
    from wsgiref.simple_server import make_server
    port = int(os.environ.get("PORT", 8000))
    print(f"Starting Driver Drowsiness Detection API server at http://127.0.0.1:{port}")
    server = make_server("0.0.0.0", port, application)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
