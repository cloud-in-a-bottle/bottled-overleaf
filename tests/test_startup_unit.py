"""Startup behavior without building the large upstream Overleaf image."""

import http.client
import threading
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import auth_proxy
import bootstrap_admin


@contextmanager
def proxy(cred_file: Path):
    class Handler(auth_proxy.AuthProxyHandler):
        pass

    Handler.cred_file = str(cred_file)
    server = auth_proxy.IPv4ThreadingServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def get(port, path="/project", headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers=headers or {})
    response = conn.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    conn.close()
    return result


def test_owner_waits_for_bootstrap_then_auto_logs_in(tmp_path):
    creds = tmp_path / "admin-credentials.txt"
    owner = {"X-OpenHost-Is-Owner": "true", "Accept": "text/html"}
    with proxy(creds) as port, patch.object(auth_proxy, "_login_to_overleaf", return_value="overleaf.sid=new; Path=/") as login:
        status, headers, body = get(port, headers=owner)
        assert status == 503
        assert b'http-equiv="refresh" content="3"' in body
        assert headers["Cache-Control"] == "no-store"
        assert headers["Retry-After"] == "3"
        assert get(port, "/login", owner)[0] == 503
        assert get(port, "/login?after_logout=true", {**owner, "Cookie": "overleaf.sid=stale"})[0] == 503
        login.assert_not_called()

        creds.write_text("OVERLEAF_ADMIN_EMAIL=admin@example.com\nOVERLEAF_ADMIN_PASSWORD=secret\n")
        status, headers, body = get(port, headers=owner)
        assert (status, headers["Location"], body) == (302, "/project", b"")
        assert headers["Set-Cookie"].startswith("overleaf.sid=new;")
        assert get(port, "/login", owner)[1]["Location"] == "/project"
        assert login.call_count == 2


def test_startup_page_never_applies_to_nonowners_or_machine_requests(tmp_path):
    with proxy(tmp_path / "absent") as port:
        # No upstream in this unit test: these paths are forwarded rather
        # than being mistaken for owner HTML navigations.
        assert get(port, headers={"Accept": "text/html"})[0] == 502
        assert get(port, headers={"X-OpenHost-Is-Owner": "true"})[0] == 502
        assert get(port, "/api/projects", {"X-OpenHost-Is-Owner": "true", "Accept": "text/html"})[0] == 502
        assert get(port, "/_healthz")[0] == 200


def test_bootstrap_retries_transient_failure_and_stops_after_success():
    with patch.object(bootstrap_admin, "_bootstrap_once", side_effect=[RuntimeError("not ready"), None]) as run:
        with patch.object(bootstrap_admin.time, "sleep") as sleep:
            assert bootstrap_admin.main() == 0
    assert run.call_count == 2
    sleep.assert_called_once_with(10)


def test_bootstrap_exhausts_retries():
    with patch.object(bootstrap_admin, "_bootstrap_once", side_effect=RuntimeError("not ready")) as run:
        with patch.object(bootstrap_admin.time, "sleep") as sleep:
            try:
                bootstrap_admin.main()
                assert False, "expected bootstrap failure"
            except RuntimeError:
                pass
    assert run.call_count == 5
    assert sleep.call_count == 4


def test_credentials_are_only_published_after_password_is_set(tmp_path):
    creds = tmp_path / "admin-credentials.txt"

    def check_not_published(*_):
        assert not creds.exists()

    with (
        patch.object(bootstrap_admin, "CRED_FILE", str(creds)),
        patch.object(bootstrap_admin, "_wait_for_overleaf_ready"),
        patch.object(bootstrap_admin, "_wait_for_activation_ready"),
        patch.object(bootstrap_admin, "_delete_user_if_exists"),
        patch.object(bootstrap_admin, "_run_create_user", return_value=("token", "/user/activate")),
        patch.object(bootstrap_admin, "_set_password", side_effect=check_not_published),
    ):
        bootstrap_admin._bootstrap_once()
    assert creds.stat().st_mode & 0o777 == 0o600
    assert "OVERLEAF_ADMIN_PASSWORD=" in creds.read_text()
