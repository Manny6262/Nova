"""Private localhost page used to verify Nova's real browser-control loop."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json


HOST = "127.0.0.1"
PORT = 5123
STATE = {"click_count": 0}


PAGE = b"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Nova Control Audit</title>
  <style>
    html, body { height: 100%; margin: 0; font-family: Arial, sans-serif; }
    body { display: grid; place-items: center; background: #101827; color: white; }
    main { text-align: center; }
    button { width: 520px; height: 220px; font-size: 42px; font-weight: bold;
      color: white; background: #118c4f; border: 8px solid white; border-radius: 28px; }
    #status { margin-top: 30px; font-size: 44px; font-weight: bold; }
  </style>
</head>
<body>
  <main>
    <h1>Nova Private Control Test</h1>
    <button id="audit-button">CLICK THIS GREEN TEST BUTTON</button>
    <div id="status">WAITING</div>
  </main>
  <script>
    document.getElementById('audit-button').addEventListener('click', async () => {
      const response = await fetch('/clicked', {method: 'POST'});
      const result = await response.json();
      document.getElementById('status').textContent = 'CLICKED ' + result.click_count;
      document.body.style.background = '#173b22';
    });
  </script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def _json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(PAGE)))
            self.end_headers()
            self.wfile.write(PAGE)
        elif self.path == "/status":
            self._json(STATE)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path == "/clicked":
            STATE["click_count"] += 1
            self._json(STATE)
        elif self.path == "/shutdown":
            self._json({"ok": True})
            self.server.shutdown()
        else:
            self._json({"error": "not found"}, 404)

    def log_message(self, _format, *_args):
        return


if __name__ == "__main__":
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
