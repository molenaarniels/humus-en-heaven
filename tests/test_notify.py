"""Tests voor notify.run_guarded (crash-vangnet) en sanitize_error.

Pure logica: send_telegram wordt gemonkeypatcht, de crash-teller krijgt een
tmp_path. Geen netwerk.
"""

import pytest

import notify
from notify import run_guarded, sanitize_error


def _boom():
    raise RuntimeError("kapot: https://api.github.com/gists/0a1b2c3d4e5f60718293a4b5?token=geheim")


@pytest.fixture
def sent(monkeypatch):
    """Vang send_telegram-calls; geen echte sends."""
    calls = []
    monkeypatch.setattr(notify, "send_telegram", lambda text, **kw: calls.append(text) or True)
    monkeypatch.delenv("DRY_RUN", raising=False)
    return calls


def test_alert_en_exit_bij_crash(sent, tmp_path):
    counter = str(tmp_path / "c")
    with pytest.raises(SystemExit) as exc:
        run_guarded(_boom, "testpijplijn", counter_file=counter)
    assert exc.value.code == 1
    assert len(sent) == 1
    # Gesanitized: Gist-ID en token-param weggepoetst.
    assert "0a1b2c3d4e5f60718293a4b5" not in sent[0]
    assert "geheim" not in sent[0]
    assert "testpijplijn" in sent[0]


def test_throttle_alert_pas_bij_drempel(sent, tmp_path):
    counter = str(tmp_path / "c")
    for verwacht_aantal in (0, 0, 1):  # drempel 3 → alert precies bij de 3e
        with pytest.raises(SystemExit):
            run_guarded(_boom, "loop", fail_threshold=3, counter_file=counter)
        assert len(sent) == verwacht_aantal
    # 4e opeenvolgende crash: géén herhaalspam (alleen de eerste overschrijding).
    with pytest.raises(SystemExit):
        run_guarded(_boom, "loop", fail_threshold=3, counter_file=counter)
    assert len(sent) == 1


def test_succes_reset_teller(sent, tmp_path):
    counter = str(tmp_path / "c")
    for _ in range(2):
        with pytest.raises(SystemExit):
            run_guarded(_boom, "loop", fail_threshold=3, counter_file=counter)
    run_guarded(lambda: None, "loop", fail_threshold=3, counter_file=counter)
    assert not (tmp_path / "c").exists()
    # Na de reset begint de telling opnieuw: 2 nieuwe crashes → nog geen alert.
    for _ in range(2):
        with pytest.raises(SystemExit):
            run_guarded(_boom, "loop", fail_threshold=3, counter_file=counter)
    assert sent == []


def test_dry_run_verstuurt_niet(sent, tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "1")
    with pytest.raises(SystemExit):
        run_guarded(_boom, "test", counter_file=str(tmp_path / "c"))
    assert sent == []


def test_system_exit_passeert_ongemoeid(sent, tmp_path):
    def bewuste_exit():
        raise SystemExit(2)
    with pytest.raises(SystemExit) as exc:
        run_guarded(bewuste_exit, "test", counter_file=str(tmp_path / "c"))
    assert exc.value.code == 2
    assert sent == []


def test_html_escape_in_alert(sent, tmp_path):
    def boom_html():
        raise RuntimeError("<Response [500]>")
    with pytest.raises(SystemExit):
        run_guarded(boom_html, "test", counter_file=str(tmp_path / "c"))
    assert "<Response" not in sent[0]
    assert "&lt;Response [500]&gt;" in sent[0]


def test_sanitize_error_patronen():
    e = Exception(
        "https://api.weather.com/x?stationId=IABC1&apiKey=k123 "
        "https://api.telegram.org/bot12345:AAH-xyz/sendMessage "
        "https://api.github.com/gists/0123456789abcdef01"
    )
    out = sanitize_error(e)
    for geheim in ("IABC1", "k123", "12345:AAH-xyz", "0123456789abcdef01"):
        assert geheim not in out
