"""Non-sensitive local pages for the P4A Personal Skill E2E matrix."""

from __future__ import annotations

import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/slow":
            time.sleep(45)
        pages = {
            "/": ("P4A Fixture", "This is a controlled local page for Personal Skill end-to-end testing. It contains only public synthetic test content."),
            "/empty": ("P4A Empty", "Hi."),
            "/login": ("Sign in", "Please log in with an account to view this private page. Username and password fields are intentionally absent in this synthetic fixture."),
            "/challenge": ("Security verification", "Verify you are human. This synthetic fixture is a classification test; there is no challenge to solve."),
            "/blocked": ("Access denied", "Request blocked. This synthetic fixture only tests the observed page status and does not assert a cause."),
            "/form": ("P4A Form", '<label for="note">Synthetic note</label><input id="note" value=""><p>This form is a controlled local test target for tab-handle validation. Entered values stay in this browser page and are never submitted.</p>'),
            "/slow": ("P4A Slow", "This page waited 45 seconds before responding. It is synthetic non-sensitive test content."),
        }
        if path == "/seed":
            self.send_response(302)
            self.send_header("Set-Cookie", "p4a_fixture=logged_in; Path=/; SameSite=Lax")
            self.send_header("Location", "/private")
            self.end_headers()
            return
        if path == "/clear":
            self.send_response(302)
            self.send_header("Set-Cookie", "p4a_fixture=; Path=/; Max-Age=0; SameSite=Lax")
            self.send_header("Location", "/private")
            self.end_headers()
            return
        if path == "/private":
            if "p4a_fixture=logged_in" in self.headers.get("Cookie", ""):
                title = "P4A Private"
                body = "AUTHENTICATED_LOCAL_SESSION_2026. This is a synthetic local login-state marker with no personal data. It proves this browser holds the fixture cookie."
            else:
                title = "Sign in"
                body = "Please log in with an account to view this private page. This synthetic fixture contains no real credentials."
        elif path in pages:
            title, body = pages[path]
        else:
            self.send_error(404)
            return
        html = f"<!doctype html><html><head><title>{title}</title></head><body><main><h1>{title}</h1><p>{body}</p></main></body></html>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        try:
            self.wfile.write(html)
        except (BrokenPipeError, ConnectionResetError):
            # The /slow client is expected to disconnect when the browser times out.
            pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8767), Handler).serve_forever()
