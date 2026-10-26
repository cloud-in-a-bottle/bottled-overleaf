import time

from openhost_test_harness import OpenhostStack


def test_app_serves_owner(stack: OpenhostStack) -> None:
    # The app is marked running once its health check passes; "/" can take a little longer to come up.
    deadline = time.monotonic() + 60
    while True:
        response = stack.owner_session.get(f"{stack.url}/", timeout=30)
        if response.status_code < 400 or time.monotonic() > deadline:
            break
        time.sleep(2)
    assert response.status_code < 400, (
        f"GET / returned {response.status_code}: {response.text[:500]}\n\napp logs:\n{stack.app_logs()[-5000:]}"
    )


def test_owner_auto_login(stack: OpenhostStack) -> None:
    # The admin account is bootstrapped in the background once Overleaf is up; until then the proxy can't log the
    # owner in and Overleaf redirects /project to /login.
    deadline = time.monotonic() + 300
    while True:
        response = stack.owner_session.get(f"{stack.url}/project", headers={"Accept": "text/html"}, timeout=30)
        logged_in = response.status_code == 200 and "/login" not in response.url
        if logged_in or time.monotonic() > deadline:
            break
        time.sleep(5)
    assert logged_in, (
        f"GET /project ended at {response.url} ({response.status_code})\n\napp logs:\n{stack.app_logs()[-5000:]}"
    )
