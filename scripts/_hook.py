import http.server, json, pathlib
LOG = pathlib.Path(r"G:\ai_project\全球拨测监控项目\globe-probe\artifacts\hook.log")
LOG.parent.mkdir(parents=True, exist_ok=True)
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n).decode("utf-8", "replace")
        with LOG.open("a", encoding="utf-8") as f:
            f.write(body + chr(10))
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(b'{"ok":true}')
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", 8699), H).serve_forever()