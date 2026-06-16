"""Tests voor gist_io — gedeelde read-only Gist-helpers.

read_file raist bij netwerk-/HTTP-fouten (caller beslist); read_json is de
gracieuze variant die bij élke fout `default` teruggeeft. requests wordt
gemockt — geen netwerk.
"""

import pytest
import requests

import gist_io


class _Resp:
    def __init__(self, payload, status_ok=True):
        self._payload = payload
        self._status_ok = status_ok

    def raise_for_status(self):
        if not self._status_ok:
            raise requests.HTTPError("404")

    def json(self):
        return self._payload


def _gist(files: dict) -> dict:
    """Bouw een Gist-API-respons met {filename: {"content": ...}}."""
    return {"files": {name: {"content": c} for name, c in files.items()}}


def test_read_file_geeft_content(monkeypatch):
    monkeypatch.setattr(gist_io.requests, "get",
                        lambda *a, **k: _Resp(_gist({"x.json": "hallo"})))
    assert gist_io.read_file("gid", "x.json") == "hallo"


def test_read_file_ontbrekend_bestand_geeft_none(monkeypatch):
    monkeypatch.setattr(gist_io.requests, "get",
                        lambda *a, **k: _Resp(_gist({"ander.json": "{}"})))
    assert gist_io.read_file("gid", "x.json") is None


def test_read_file_raist_bij_http_fout(monkeypatch):
    monkeypatch.setattr(gist_io.requests, "get",
                        lambda *a, **k: _Resp({}, status_ok=False))
    with pytest.raises(requests.HTTPError):
        gist_io.read_file("gid", "x.json")


def test_read_file_stuurt_bearer_token_mee(monkeypatch):
    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["headers"] = headers
        return _Resp(_gist({"x.json": "1"}))

    monkeypatch.setattr(gist_io.requests, "get", fake_get)
    gist_io.read_file("gid", "x.json", token="secret")
    assert captured["headers"]["Authorization"] == "Bearer secret"


def test_read_json_parset_inhoud(monkeypatch):
    monkeypatch.setattr(gist_io.requests, "get",
                        lambda *a, **k: _Resp(_gist({"d.json": '{"a": 1}'})))
    assert gist_io.read_json("gid", "d.json") == {"a": 1}


def test_read_json_default_bij_ontbrekend_bestand(monkeypatch):
    monkeypatch.setattr(gist_io.requests, "get",
                        lambda *a, **k: _Resp(_gist({"ander.json": "{}"})))
    assert gist_io.read_json("gid", "d.json", default={}) == {}


def test_read_json_default_bij_lege_content(monkeypatch):
    monkeypatch.setattr(gist_io.requests, "get",
                        lambda *a, **k: _Resp(_gist({"d.json": ""})))
    assert gist_io.read_json("gid", "d.json", default={"fallback": True}) == {"fallback": True}


def test_read_json_slikt_netwerkfout_en_geeft_default(monkeypatch):
    def kapot(*a, **k):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(gist_io.requests, "get", kapot)
    # Mag niet raisen: read_json is de gracieuze variant.
    assert gist_io.read_json("gid", "d.json", default=[]) == []


def test_read_json_default_bij_kapotte_json(monkeypatch):
    monkeypatch.setattr(gist_io.requests, "get",
                        lambda *a, **k: _Resp(_gist({"d.json": "{niet: geldig"})))
    assert gist_io.read_json("gid", "d.json", default=None) is None
