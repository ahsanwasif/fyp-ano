"""Minimal Alertmanager webhook receiver: prints alerts. In ANO this is where an alert becomes a new intent."""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        for a in body.get("alerts", []):
            print(f"[{a['status'].upper()}] {a['labels'].get('alertname')}: {a['annotations'].get('summary')}")
        self.send_response(200)
        self.end_headers()


if __name__ == "__main__":
    print("listening on :5001")
    HTTPServer(("0.0.0.0", 5001), H).serve_forever()
