import json
import threading

import pytest
import requests

from loopedin import ui
from loopedin.config import Account, Config


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOPEDIN_HOME", str(tmp_path))
    monkeypatch.setattr(ui, "delete_secret", lambda name: None)  # keep tests out of Credential Manager
    srv, token = ui.make_server(port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield base, token
    srv.shutdown()
    srv.server_close()


def test_page_embeds_token(server):
    base, token = server
    page = requests.get(base + "/").text
    assert f'const TOKEN = "{token}"' in page


def test_api_rejects_missing_token(server):
    base, _ = server
    assert requests.get(base + "/api/accounts").status_code == 403


def test_api_rejects_foreign_host(server):
    base, token = server
    resp = requests.get(base + "/api/accounts", headers={"X-Loopedin-Token": token, "Host": "evil.example:80"})
    assert resp.status_code == 403
    assert requests.get(base + "/", headers={"Host": "evil.example"}).status_code == 403


def test_accounts_roundtrip_and_remove(server):
    base, token = server
    config = Config()
    config.add(Account("gmail", "me@gmail.com"))
    config.save()
    h = {"X-Loopedin-Token": token}
    data = requests.get(base + "/api/accounts", headers=h).json()
    assert data["accounts"] == [{"provider": "gmail", "address": "me@gmail.com"}]

    resp = requests.post(base + "/api/accounts/remove", headers=h,
                         data=json.dumps({"provider": "gmail", "address": "me@gmail.com"}))
    assert resp.json() == {"ok": True}
    assert Config.load().accounts == []


def test_gmail_validates_before_contacting_google(server):
    base, token = server
    resp = requests.post(base + "/api/gmail", headers={"X-Loopedin-Token": token},
                         data=json.dumps({"address": "not-an-email", "password": "x"}))
    assert resp.status_code == 400


def test_emails_with_no_accounts_is_empty_and_bad_window_is_400(server):
    base, token = server
    h = {"X-Loopedin-Token": token}
    assert requests.get(base + "/api/emails?since=24h", headers=h).json() == {"results": []}
    assert requests.get(base + "/api/emails?since=soon", headers=h).status_code == 400
