import json
import logging
import time
from unittest.mock import ANY

import pytest

P = "/v1/settings/providers"
M = "/v1/settings/models"
SMALL = "liquid/lfm3-1.7b"


@pytest.fixture
def admin(make_client):
    return make_client(client_host="127.0.0.1")


def provider(client, name):
    return next(p for p in client.get(P).json()["data"] if p["name"] == name)


def chat(client, model="auto", content="hey, how are you?"):
    return client.post("/v1/chat/completions", json={"model": model, "messages": [{"role": "user", "content": content}]})


def wait_for_trace(client, trace_id, tries=50):
    for _ in range(tries):
        r = client.get(f"/v1/traces/{trace_id}")
        if r.status_code == 200:
            return r.json()
        time.sleep(0.02)
    raise AssertionError(f"trace {trace_id} was never recorded")


def test_access_rules(make_client):
    remote = make_client()
    r = remote.get("/v1/settings")
    assert r.status_code == 403 and r.json()["error"]["code"] == "settings_forbidden"
    assert "JEV_ROUTER_ADMIN_KEY" in r.json()["error"]["message"]
    local = make_client(client_host="127.0.0.1")
    body = local.get("/v1/settings").json()
    assert body["access"] == "loopback" and body["secret_source"] == "env" and body["warnings"] == []
    assert local.get("/v1/settings", headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 403
    assert local.get(P, headers={"Forwarded": "for=1.2.3.4"}).status_code == 403
    keyed = make_client(client_host="127.0.0.1", jev_router_admin_key="admin-secret-1", jev_router_api_keys="router-key-1")
    assert keyed.get("/v1/settings").status_code == 403
    assert keyed.get("/v1/settings", headers={"Authorization": "Bearer wrong"}).status_code == 403
    assert keyed.get("/v1/settings", headers={"Authorization": "Bearer router-key-1"}).status_code == 403
    ok = keyed.get("/v1/settings", headers={"x-api-key": "admin-secret-1"})
    assert ok.status_code == 200 and ok.json()["access"] == "admin_key"


def test_settings_rejects_foreign_origin_and_host(make_client):
    local = make_client(client_host="127.0.0.1")
    r = local.get("/v1/settings", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "settings_forbidden"
    assert local.patch(P + "/openai", json={"api_key": "sk-evil-000111222"}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert local.get("/v1/settings", headers={"Origin": "http://localhost:8787"}).status_code == 200
    assert local.get("/v1/settings", headers={"Origin": "http://127.0.0.1:8787", "Host": "127.0.0.1:8787"}).status_code == 200
    assert local.get("/v1/settings", headers={"Origin": "http://[::1]:8787", "Host": "[::1]:8787"}).status_code == 200
    assert local.get("/v1/settings", headers={"Host": "evil.example"}).status_code == 403
    assert local.get("/v1/settings", headers={"Host": "evil.example:8787"}).status_code == 403
    assert local.get("/v1/settings", headers={"Origin": "null"}).status_code == 403
    assert local.get("/v1/settings", headers={"Origin": "http://[::1"}).status_code == 403
    assert make_client(client_host="::ffff:127.0.0.1").get("/v1/settings").status_code == 200
    assert make_client(client_host="::ffff:10.0.0.5").get("/v1/settings").status_code == 403
    remote = make_client(client_host="203.0.113.5", jev_router_admin_key="admin-secret-1")
    assert remote.get("/v1/settings", headers={"Authorization": "Bearer admin-secret-1"}).status_code == 200


@pytest.mark.parametrize("body,fragment", [
    ({"name": "Bad Name", "base_url": "http://x/v1"}, "name"),
    ({"name": "lab", "base_url": "ftp://x"}, "base_url"),
    ({"name": "lab", "base_url": "http:///v1"}, "base_url"),
    ({"name": "lab", "base_url": "http://x/v1", "headers": {"Authorization": "Bearer a"}}, "headers"),
    ({"name": "lab", "base_url": "http://x/v1", "headers": {"bad header": "v"}}, "headers"),
    ({"name": "lab", "base_url": "http://x/v1", "headers": {"X-Big": "v" * 2049}}, "headers"),
    ({"name": "lab", "base_url": "http://x/v1", "surprise": 1}, "surprise"),
    ({"name": "lab", "base_url": "http://x/v1", "api_key": "lab-key-0001\n"}, "api_key: Value error, must contain only printable ASCII"),
    ({"name": "lab", "base_url": "http://x/v1", "api_key": "lab-key-caf\u00e9"}, "api_key: Value error, must contain only printable ASCII"),
    ({"name": "lab", "base_url": "http://x/v1", "headers": {"X-A": "v\r\nX-B: y"}}, "headers"),
    ({"name": "lab", "base_url": "http://x/v1", "headers": {"X-Key: lab-key-0001": "v"}}, "invalid header name"),
    ({"name": "lab", "base_url": "http://x/v1", "headers": {"X-A": "1", "x-a": "2"}}, "duplicate header name"),
])
def test_provider_validation(admin, body, fragment):
    r = admin.post(P, json=body)
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_request"
    assert fragment in r.json()["error"]["message"]
    assert "lab-key-" not in r.text and "X-B: y" not in r.text


def test_conflicts_unknowns_and_model_validation(admin):
    assert admin.post(P, json={"name": "openai", "base_url": "http://x/v1"}).status_code == 409
    assert admin.patch(P + "/ghost", json={"enabled": True}).status_code == 404
    assert admin.patch(P + "/openai", json={"headers": {"X-A": "b"}}).status_code == 400
    assert admin.patch(P + "/openai", json={"enabled": None}).status_code == 400
    assert admin.patch(P + "/openai", json={}).status_code == 400
    assert admin.get(P + "/ghost/discover").status_code == 404
    assert admin.post(M, json=[{"provider": "liquid", "model": "lfm3-1.7b"}]).status_code == 409
    assert admin.post(M, json=[{"provider": "ghost", "model": "x"}]).status_code == 400
    assert admin.post(M, json=[]).status_code == 400
    assert admin.patch(M + "/ghost/m", json={"speed": 2}).status_code == 404
    for bad in ({"quality": 11}, {"speed": 0}, {"input_cost": -1}, {"context_window": 0},
                {"modalities": ["text", "video"]}, {"provider": "openai"}):
        r = admin.patch(M + "/" + SMALL, json=bad)
        assert r.status_code == 400, bad


@pytest.mark.parametrize("name", ["..", "../x", "a/./b", "../../providers/openai"])
def test_model_names_reject_dot_segments(admin, name):
    r = admin.post(M, json=[{"provider": "liquid", "model": name}])
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_request"
    assert "model" in r.json()["error"]["message"]


def test_model_routes_accept_an_encoded_slash(admin):
    assert admin.patch(M + "/liquid%2Flfm3-1.7b", json={"speed": 7}).status_code == 200


def test_builtin_key_and_ollama_enable(admin):
    r = admin.patch(P + "/openai", json={"api_key": "sk-test-openai-000111"})
    assert r.status_code == 200 and r.json()["available"]
    assert r.json()["key"] == {"required": True, "set": True, "source": "settings", "hint": "sk-…0111", "env": "OPENAI_API_KEY"}
    models = {m["id"]: m for m in admin.get("/v1/models").json()["data"]}
    assert models["openai/gpt-5"]["available"] and models["openai/gpt-5"]["enabled"] is True
    r = admin.patch(P + "/openai", json={"api_key": None})
    assert r.json()["key"]["set"] is False and not r.json()["available"]
    assert admin.patch(P + "/ollama", json={"enabled": True}).json()["available"]
    assert admin.get("/health").json()["providers"] == ["liquid", "ollama"]
    assert admin.delete(P + "/ollama").status_code == 204
    assert not provider(admin, "ollama")["available"]


def test_settings_persist_and_survive_a_lost_secret(make_client, tmp_path):
    db = f"sqlite+aiosqlite:///{tmp_path}/shared.db"
    first = make_client(client_host="127.0.0.1", database_url=db)
    assert first.patch(P + "/groq", json={"api_key": "gsk-persisted-key-1234"}).status_code == 200
    again = make_client(client_host="127.0.0.1", database_url=db)
    groq = provider(again, "groq")
    assert groq["key"]["source"] == "settings" and groq["available"]
    lost = make_client(client_host="127.0.0.1", database_url=db, jev_router_secret="a-different-secret")
    groq = provider(lost, "groq")
    assert groq["key"]["source"] == "unreadable" and not groq["available"]
    assert any("groq" in w for w in lost.get("/v1/settings").json()["warnings"])


def test_custom_provider_end_to_end(admin, upstream):
    upstream.listed["lab.example"] = {"models": ["qwen3-8b", "qwen3-32b", "qwen3-8b"]}
    r = admin.post(P, json={"name": "lab", "base_url": "https://lab.example/v1/", "api_key": "lab-key-abcdef123456"})
    assert r.status_code == 201
    lab = r.json()
    assert lab["base_url"] == "https://lab.example/v1" and lab["source"] == "custom" and lab["local"] is False
    assert lab["key"]["hint"] == "lab…3456" and lab["model_count"] == 0
    assert admin.post(P + "/lab/test").json() == {"ok": True, "latency_ms": ANY, "models_found": 2, "error": None}
    found = admin.get(P + "/lab/discover").json()["data"]
    assert [d["id"] for d in found] == ["lab/qwen3-8b", "lab/qwen3-32b"] and not found[0]["registered"]
    assert found[0]["suggested"]["description"] == "qwen3-8b served by lab"
    r = admin.post(M, json=[{"provider": "lab", "model": "qwen3-8b", "quality": 6, "input_cost": 0.1, "output_cost": 0.2},
                            {"provider": "lab", "model": "qwen3-32b", "quality": 8}])
    assert r.status_code == 201 and [m["id"] for m in r.json()["data"]] == ["lab/qwen3-8b", "lab/qwen3-32b"]
    assert r.json()["data"][0]["source"] == "custom" and r.json()["data"][0]["available"]
    assert admin.get(P + "/lab/discover").json()["data"][0]["registered"]
    models = {m["id"]: m for m in admin.get("/v1/models").json()["data"]}
    assert models["lab/qwen3-8b"]["available"] and models["lab/qwen3-8b"]["owned_by"] == "lab"
    r = chat(admin, model="lab/qwen3-8b")
    assert r.status_code == 200 and r.json()["model"] == "lab/qwen3-8b"
    sent = upstream.chat_requests[-1]
    assert sent.url.host == "lab.example" and sent.headers["authorization"] == "Bearer lab-key-abcdef123456"
    assert json.loads(sent.content)["model"] == "qwen3-8b"
    assert admin.delete(P + "/lab").status_code == 204
    ids = {m["id"] for m in admin.get("/v1/models").json()["data"]}
    assert not {"lab/qwen3-8b", "lab/qwen3-32b"} & ids


def test_model_patch_clears_decision_cache_and_prunes(make_client, upstream):
    c = make_client(client_host="127.0.0.1", decision_cache_ttl=60)
    upstream.route_choice, upstream.complexity, upstream.premium, upstream.task = SMALL, 0.2, 0.02, "chat"
    assert chat(c).status_code == 200
    assert c.get("/health").json()["decision_cache"]["size"] >= 1
    r = c.patch(M + "/" + SMALL, json={"speed": 7})
    assert r.status_code == 200 and r.json()["overridden"] == ["speed"] and r.json()["speed"] == 7
    assert c.get("/health").json()["decision_cache"]["size"] == 0
    assert c.patch(M + "/" + SMALL, json={"speed": 9}).json()["overridden"] == []
    c.patch(M + "/" + SMALL, json={"enabled": False})
    assert next(m for m in c.get("/v1/models").json()["data"] if m["id"] == SMALL)["enabled"] is False
    assert c.delete(M + "/" + SMALL).status_code == 204
    assert next(m for m in c.get(M).json()["data"] if m["id"] == SMALL)["enabled"] is True


def test_ollama_discovery_falls_back_to_tags(admin, upstream):
    upstream.listed["localhost"] = {"tags": ["llama3.2:3b", "qwen3:8b"]}
    data = admin.get(P + "/ollama/discover").json()["data"]
    assert [(d["model"], d["id"], d["registered"]) for d in data] == [
        ("llama3.2:3b", "ollama/llama3.2:3b", True), ("qwen3:8b", "ollama/qwen3:8b", False)]
    assert admin.post(P + "/test", json={"type": "openai", "base_url": "http://localhost:11434/v1"}).json()["models_found"] == 2


def test_discovery_failures_are_scrubbed(admin, upstream):
    upstream.listed["lab.example"] = {"status": 401, "message": "bad key lab-key-abcdef123456"}
    admin.post(P, json={"name": "lab", "base_url": "https://lab.example/v1", "api_key": "lab-key-abcdef123456"})
    r = admin.get(P + "/lab/discover")
    assert r.status_code == 502 and r.json()["error"]["code"] == "discovery_failed"
    assert "HTTP 401" in r.text and "lab-key-abcdef123456" not in r.text
    t = admin.post(P + "/lab/test", json={"api_key": "lab-key-abcdef123456"}).json()
    assert t["ok"] is False and "lab-key-abcdef123456" not in t["error"]
    unsaved = admin.post(P + "/test", json={"type": "openai", "base_url": "https://nowhere.example/v1"}).json()
    assert unsaved["ok"] is False and "HTTP 404" in unsaved["error"]


def test_probe_with_new_base_url_does_not_send_stored_secrets(admin, upstream):
    key, header = "lab-key-abcdef123456", "hdr-team-value-7788"
    admin.post(P, json={"name": "lab", "base_url": "https://lab.example/v1", "api_key": key, "headers": {"X-Team": header}})
    upstream.listed["evil.example"] = {"models": ["m"]}
    upstream.listed["lab.example"] = {"models": ["m"]}
    assert admin.post(P + "/lab/test", json={"base_url": "https://evil.example/v1"}).json()["ok"] is True
    sent = upstream.list_requests[-1]
    assert sent.url.host == "evil.example" and "authorization" not in sent.headers and "x-team" not in sent.headers
    assert admin.post(P + "/lab/test", json={"base_url": "https://lab.example/v1/"}).json()["ok"] is True
    sent = upstream.list_requests[-1]
    assert sent.url.host == "lab.example" and sent.headers["authorization"] == f"Bearer {key}"
    assert sent.headers["x-team"] == header


def test_secrets_never_leave_the_server(admin, upstream, caplog):
    caplog.set_level(logging.INFO)
    key, header = "sk-leakcheck-key-998877", "hdr-leakcheck-value-5566"
    assert admin.post(P, json={"name": "lab", "base_url": "https://lab.example/v1", "api_key": key,
                               "headers": {"X-Team": header}}).json()["headers"] == {"X-Team": "••••"}
    admin.post(M, json=[{"provider": "lab", "model": "m1"}])
    admin.patch(P + "/openai", json={"api_key": key})
    r = chat(admin, model="lab/m1")
    assert r.status_code == 200 and upstream.chat_requests[-1].headers["x-team"] == header
    trace = wait_for_trace(admin, r.headers["x-jev-router-trace-id"])
    export = admin.get("/v1/settings/export")
    assert export.status_code == 200 and export.headers["content-type"].startswith("text/yaml")
    assert "lab/m1" in export.text and "LAB_API_KEY" in export.text
    bodies = [admin.get(u).text for u in ("/v1/settings", P, M, "/v1/models", "/health")]
    bodies += [export.text, json.dumps(trace), caplog.text]
    for b in bodies:
        assert key not in b and header not in b
