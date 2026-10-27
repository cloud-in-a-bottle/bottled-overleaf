"""Tests for what this package changes on top of the upstream Overleaf CE image (beyond test_smoke.py's owner login).

- The bootstrapped account the owner is signed in as is an Overleaf site admin.
- public_paths = ["/"]: anonymous visitors reach Overleaf but are never auto-signed-in (the auth proxy keys on the
  router-stamped owner header, which the router strips from client requests).
- Anonymous link sharing is enabled, so a share link opens the project for a visitor with no account.
- Compiles work: start.sh relaxes clsi-nginx's disable_symlinks because /var/lib/overleaf is a symlink into app data.
"""

import re
import time
from collections.abc import Iterator

import pytest
import requests
from openhost_test_harness import OpenhostStack

HTML = {"Accept": "text/html"}


def _logs(stack: OpenhostStack) -> str:
    return f"\n\napp logs:\n{stack.app_logs()[-5000:]}"


def _csrf(html: str) -> str:
    match = re.search(r'<meta\s+name="ol-csrfToken"\s+content="([^"]+)"', html)
    assert match, f"no ol-csrfToken on page: {html[:500]}"
    return match.group(1)


def _owner_page(stack: OpenhostStack, path: str) -> requests.Response:
    """Load a page as the owner, waiting for the background admin bootstrap so the proxy can sign them in."""
    deadline = time.monotonic() + 300
    while True:
        try:
            response = stack.owner_session.get(f"{stack.url}{path}", headers=HTML, timeout=30)
            if (response.status_code == 200 and "/login" not in response.url) or time.monotonic() > deadline:
                return response
        except requests.ConnectionError:
            if time.monotonic() > deadline:
                raise
        time.sleep(5)


@pytest.fixture(scope="module")
def owner_csrf(stack: OpenhostStack) -> str:
    page = _owner_page(stack, "/project")
    assert page.status_code == 200 and "/login" not in page.url, f"owner not signed in: {page.url}{_logs(stack)}"
    return _csrf(page.text)


@pytest.fixture(scope="module")
def project_id(stack: OpenhostStack, owner_csrf: str) -> Iterator[str]:
    response = stack.owner_session.post(
        f"{stack.url}/project/new",
        json={"projectName": f"harness-{int(time.time())}", "template": "example"},
        headers={"X-Csrf-Token": owner_csrf},
        timeout=60,
    )
    assert response.status_code == 200, f"creating a project failed: {response.status_code}: {response.text[:300]}"
    yield response.json()["project_id"]


def test_owner_is_site_admin(stack: OpenhostStack, owner_csrf: str) -> None:
    response = stack.owner_session.get(f"{stack.url}/admin", headers=HTML, timeout=30)
    assert response.status_code == 200 and "/login" not in response.url, (
        f"owner can't open the admin panel: {response.status_code} at {response.url}"
    )


@pytest.mark.parametrize("forged_owner_header", [False, True])
def test_anonymous_visitor_is_not_signed_in(stack: OpenhostStack, owner_csrf: str, forged_owner_header: bool) -> None:
    headers = {**HTML, "X-OpenHost-Is-Owner": "true"} if forged_owner_header else HTML
    response = requests.get(f"{stack.url}/project", headers=headers, timeout=30)
    assert response.status_code == 200 and "/login" in response.url, (
        f"anonymous visitor wasn't sent to Overleaf's login: {response.status_code} at {response.url}"
    )


def test_share_link_opens_project_anonymously(stack: OpenhostStack, owner_csrf: str, project_id: str) -> None:
    response = stack.owner_session.post(
        f"{stack.url}/project/{project_id}/settings/admin",
        json={"publicAccessLevel": "tokenBased"},
        headers={"X-Csrf-Token": owner_csrf},
        timeout=30,
    )
    assert response.status_code in (200, 204), (
        f"enabling link sharing failed: {response.status_code}: {response.text[:300]}"
    )
    tokens = stack.owner_session.get(f"{stack.url}/project/{project_id}/tokens", timeout=30)
    assert tokens.status_code == 200, f"reading share tokens failed: {tokens.status_code}: {tokens.text[:300]}"
    token = tokens.json()["readAndWrite"]

    visitor = requests.Session()
    page = visitor.get(f"{stack.url}/{token}", headers=HTML, timeout=30)
    assert page.status_code == 200, f"share link page: {page.status_code} at {page.url}"
    grant = visitor.post(f"{stack.url}/{token}/grant", json={}, headers={"X-Csrf-Token": _csrf(page.text)}, timeout=30)
    assert grant.status_code == 200, f"share link grant: {grant.status_code}: {grant.text[:300]}"
    redirect = grant.json().get("redirect")
    assert redirect == f"/project/{project_id}", f"share link didn't grant anonymous access: {grant.json()}"
    editor = visitor.get(f"{stack.url}{redirect}", headers=HTML, timeout=30)
    assert editor.status_code == 200 and editor.url.rstrip("/").endswith(f"/project/{project_id}"), (
        f"anonymous visitor couldn't open the shared project: {editor.status_code} at {editor.url}"
    )


def test_compile_produces_pdf(stack: OpenhostStack, owner_csrf: str, project_id: str) -> None:
    response = stack.owner_session.post(
        f"{stack.url}/project/{project_id}/compile",
        json={"check": "silent", "incrementalCompilesEnabled": True},
        headers={"X-Csrf-Token": owner_csrf},
        timeout=300,
    )
    assert response.status_code == 200, f"compile request failed: {response.status_code}: {response.text[:300]}"
    result = response.json()
    assert result.get("status") == "success", f"compile failed: {str(result)[:1000]}{_logs(stack)}"
    pdf = next((f for f in result.get("outputFiles", []) if f.get("path") == "output.pdf"), None)
    assert pdf, f"no output.pdf in compile result: {result.get('outputFiles')}"
    download = stack.owner_session.get(f"{stack.url}{pdf['url']}", timeout=60)
    assert download.status_code == 200 and download.content.startswith(b"%PDF"), (
        f"PDF download failed: {download.status_code}: {download.content[:200]!r}{_logs(stack)}"
    )
