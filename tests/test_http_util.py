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


def test_default_venster_vijf_pogingen(monkeypatch):
    """De default moet een korte storingsburst overleven: 5 pogingen,
    backoff 3+8+30+60s (~100s venster i.p.v. ~11s)."""
    pogingen = {"n": 0}
    slaap = []

    def kapot(url, params=None, timeout=None):
        pogingen["n"] += 1
        raise requests.ConnectionError("down")

    monkeypatch.setattr(http_util.requests, "get", kapot)
    monkeypatch.setattr(http_util.time, "sleep", slaap.append)
    with pytest.raises(requests.ConnectionError):
        http_util.get_json("https://x")
    assert pogingen["n"] == 5
    assert slaap == [3, 8, 30, 60]


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


class _FakeResponse:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


def test_gemengde_faalvolgorde_verzamelt_alle_tags(monkeypatch):
    """Een 429 tussen timeouts moet zichtbaar blijven i.p.v. overschreven
    worden door de laatste (andere) poging — dat is precies de evidentie die
    onderscheid rate-limiting van een timeout/storing."""
    fouten = iter([
        requests.ConnectTimeout("connect traag"),
        requests.ReadTimeout("read traag"),
        requests.HTTPError("429", response=_FakeResponse(429, {"Retry-After": "30"})),
        requests.ConnectionError("reset"),
        requests.ReadTimeout("read traag weer"),
    ])

    def kapot(url, params=None, timeout=None):
        raise next(fouten)

    monkeypatch.setattr(http_util.requests, "get", kapot)
    monkeypatch.setattr(http_util.time, "sleep", lambda s: None)
    with pytest.raises(requests.ReadTimeout) as exc:
        http_util.get_json("https://x")
    note = " ".join(exc.value.__notes__)
    assert "ConnectTimeout" in note
    assert "ReadTimeout" in note
    assert "HTTP 429" in note
    assert "Retry-After=30" in note
    assert "ConnectionError" in note
    # Laatste poging blijft het geraiste type (ongewijzigd gedrag) — de
    # bestaande `except requests.RequestException` in weather_briefing.py
    # blijft dus werken.
    assert type(exc.value) is requests.ReadTimeout


def test_http_429_surfaced_met_status_en_retry_after(monkeypatch):
    def kapot(url, params=None, timeout=None):
        raise requests.HTTPError("429", response=_FakeResponse(429, {"Retry-After": "30"}))

    monkeypatch.setattr(http_util.requests, "get", kapot)
    monkeypatch.setattr(http_util.time, "sleep", lambda s: None)
    with pytest.raises(requests.HTTPError) as exc:
        http_util.get_json("https://x", attempts=1)
    note = " ".join(exc.value.__notes__)
    assert "HTTP 429" in note
    assert "Retry-After=30" in note
