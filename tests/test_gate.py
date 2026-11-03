# -*- coding: utf-8 -*-
"""Tests for the opt-in UC_ACCESS_TOKEN gate inside nonlocal/app/webapp.py.

The default (env unset) must behave exactly like the local tool: open.
"""

import base64

import pytest

from app import webapp


CLIENT = webapp.app.test_client()


def _basic(user="", pw=""):
    blob = base64.b64encode(("%s:%s" % (user, pw)).encode()).decode()
    return {"Authorization": "Basic " + blob}


def _bearer(tok):
    return {"Authorization": "Bearer " + tok}


def test_open_when_token_unset(monkeypatch):
    monkeypatch.delenv("UC_ACCESS_TOKEN", raising=False)
    assert CLIENT.get("/api/health").status_code == 200
    assert CLIENT.get("/").status_code == 200


def test_token_set_rejects_anonymous_and_probes_health(monkeypatch):
    monkeypatch.setenv("UC_ACCESS_TOKEN", "s3cret")
    r = CLIENT.get("/")
    assert r.status_code == 401
    assert "Basic" in r.headers["WWW-Authenticate"]
    assert r.get_json()["error"] == "unauthorized"
    # health probe stays open even behind the gate
    assert CLIENT.get("/api/health").status_code == 200


def test_basic_with_token_as_username(monkeypatch):
    monkeypatch.setenv("UC_ACCESS_TOKEN", "s3cret")
    assert CLIENT.get("/", headers=_basic(user="s3cret")).status_code == 200
    assert CLIENT.get("/", headers=_basic(user="s3cret", pw="x")).status_code == 200


def test_basic_with_token_as_password(monkeypatch):
    monkeypatch.setenv("UC_ACCESS_TOKEN", "s3cret")
    assert CLIENT.get("/", headers=_basic(pw="s3cret")).status_code == 200


def test_basic_wrong_token_rejected(monkeypatch):
    monkeypatch.setenv("UC_ACCESS_TOKEN", "s3cret")
    assert CLIENT.get("/", headers=_basic(user="wrong")).status_code == 401
    assert CLIENT.get("/", headers=_basic(pw="wrong")).status_code == 401


def test_bearer_token(monkeypatch):
    monkeypatch.setenv("UC_ACCESS_TOKEN", "s3cret")
    assert CLIENT.get("/", headers=_bearer("s3cret")).status_code == 200
    assert CLIENT.get("/", headers=_bearer("nope")).status_code == 401


def test_garbled_basic_header_rejected(monkeypatch):
    monkeypatch.setenv("UC_ACCESS_TOKEN", "s3cret")
    bad = {"Authorization": "Basic !!!!not-b64!!!!"}
    assert CLIENT.get("/", headers=bad).status_code == 401
