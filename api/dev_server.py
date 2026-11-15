import argparse
import json
import mimetypes
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class DevHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, directory=None, functions_url=None, **kwargs):
        self.functions_url = functions_url.rstrip("/")
        super().__init__(*args, directory=directory, **kwargs)

    def do_POST(self):
        if not self.path.startswith("/api/"):
            self._send_json(404, {"ok": False, "error": {"code": "not_found", "message": "APIが見つかりません。"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"ok": False, "error": {"code": "invalid_request", "message": "リクエストサイズが正しくありません。"}})
            return
        if length < 0 or length > 2_000_000:
            self._send_json(413, {"ok": False, "error": {"code": "request_too_large", "message": "リクエストが大きすぎます。"}})
            return

        body = self.rfile.read(length)
        headers = {}
        for header in ("Content-Type", "Accept", "Cookie", "Stripe-Signature"):
            value = self.headers.get(header)
            if value:
                headers[header] = value
        upstream = Request(
            self.functions_url + self.path,
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(upstream, timeout=30) as response:
                response_body = response.read()
                self._write_upstream_response(response.status, response.headers, response_body)
        except HTTPError as response:
            response_body = response.read()
            self._write_upstream_response(response.code, response.headers, response_body)
        except URLError as error:
            if isinstance(error.reason, TimeoutError):
                self._send_json(504, {"ok": False, "error": {"code": "functions_timeout", "message": "Azure Functionsの応答がタイムアウトしました。"}})
                return
            self._send_json(502, {"ok": False, "error": {"code": "functions_unavailable", "message": "Azure Functionsが起動していません。"}})
        except TimeoutError:
            self._send_json(504, {"ok": False, "error": {"code": "functions_timeout", "message": "Azure Functionsの応答がタイムアウトしました。"}})

    def do_OPTIONS(self):
        if self.path.startswith("/api/"):
            self._send_json(405, {"ok": False, "error": {"code": "method_not_allowed", "message": "このAPIはPOSTのみ対応しています。"}})
            return
        self.send_error(405)

    def _copy_response_headers(self, headers, body_length):
        content_type = headers.get("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(body_length))
        for cookie in headers.get_all("Set-Cookie", []):
            self.send_header("Set-Cookie", cookie)
        for name in ("Cache-Control", "X-Content-Type-Options"):
            value = headers.get(name)
            if value:
                self.send_header(name, value)

    def _write_upstream_response(self, status_code, headers, body):
        self.send_response(status_code)
        self._copy_response_headers(headers, len(body))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status_code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def guess_type(self, path):
        content_type = mimetypes.guess_type(path)[0]
        if path.endswith(".js"):
            return "text/javascript"
        return content_type or "application/octet-stream"


def main():
    parser = argparse.ArgumentParser(description="Serve docs/ and proxy /api/ to Azure Functions.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--functions-url", default=os.getenv("QR_FUNCTIONS_URL", "http://127.0.0.1:7071"))
    args = parser.parse_args()
    docs_directory = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "docs"))
    handler = lambda *handler_args, **handler_kwargs: DevHandler(
        *handler_args,
        directory=docs_directory,
        functions_url=args.functions_url,
        **handler_kwargs,
    )
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Serving {docs_directory} at http://{args.host}:{args.port}")
    print(f"Proxying /api/ to {args.functions_url}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
