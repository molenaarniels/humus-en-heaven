"""Tests voor http_util.get_json (gedeelde GET→JSON met retry/backoff)."""

import pytest
import requests

import http_util


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_succes_op_derde_poging(monkeypatch):
    pogingen = {"n": 0}

    def flaky(url, params=None, timeout=None):
        pogingen["n"] += 1
        if pogingen["n"] < 3:
            raise requests.ConnectionError("hiccup")
        return _Resp({"ok": True})

    monkeypatch.setattr(http_util.requests, "get", flaky)
    monkeypatch.setattr(http_util.time, "sleep", lambda s: None)
    assert http_util.get_json("https://x", attempts=3) == {"ok": True}
    assert pogingen["n"] == 3


def test_raist_na_laatste_poging(monkeypatch):
    pogingen = {"n": 0}

    def kapot(url, params=None, timeout=None):
        pogingen["n"] += 1
        raise requests.ConnectionError("down")

    monkeypatch.setattr(http_util.requests, "get", kapot)
    monkeypatch.setattr(http_util.time, "sleep", lambda s: None)
    with pytest.raises(requests.ConnectionError):
        http_util.get_json("https://x", attempts=3)
    assert pogingen["n"] == 3
