from __future__ import annotations

from omni.skills_runtime.builtin_tools import web


def test_url_validation_rejects_private_resolution(monkeypatch):
    monkeypatch.setattr(web, "_resolved_addresses", lambda _host: {"127.0.0.1"})
    error = web._validate_url("https://allowed.example/path", ["allowed.example"])
    assert "non-public address" in error


def test_url_validation_rechecks_allowlist_and_credentials(monkeypatch):
    monkeypatch.setattr(web, "_resolved_addresses", lambda _host: {"93.184.216.34"})
    assert web._validate_url("https://allowed.example/path", ["allowed.example"]) == ""
    assert "not in allowlist" in web._validate_url(
        "https://redirected.example/path", ["allowed.example"]
    )
    assert "credentials" in web._validate_url(
        "https://user:pass@allowed.example/path", ["allowed.example"]
    )


def test_private_hosts_require_explicit_opt_in(monkeypatch):
    monkeypatch.setattr(web, "_resolved_addresses", lambda _host: {"10.0.0.3"})
    assert web._validate_url(
        "http://internal.example", ["internal.example"], allow_private=True
    ) == ""
