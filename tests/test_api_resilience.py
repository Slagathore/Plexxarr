"""Jikan retry/circuit-breaker and Plex timeout-retry resilience."""
import pytest

import media_lookup


@pytest.fixture(autouse=True)
def _reset_jikan(monkeypatch):
    # Reset breaker state and neutralize real sleeps/throttle so tests are fast.
    media_lookup._jikan_consecutive_failures = 0
    media_lookup._jikan_circuit_until = 0.0
    monkeypatch.setattr(media_lookup, "_jikan_throttle", lambda: None)
    monkeypatch.setattr(media_lookup.time, "sleep", lambda *_a: None)
    yield
    media_lookup._jikan_consecutive_failures = 0
    media_lookup._jikan_circuit_until = 0.0


def test_jikan_retries_5xx_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def flaky(url, **kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError(f"HTTP 504 from {url}")
        return {"data": ["ok"]}

    monkeypatch.setattr(media_lookup, "_get_json", flaky)
    result = media_lookup._jikan_get("https://api.jikan.moe/v4/anime?q=x")
    assert result == {"data": ["ok"]}
    assert calls["n"] == 3  # two 504s retried, third succeeded


def test_jikan_circuit_opens_after_repeated_failures(monkeypatch):
    calls = {"n": 0}

    def always_504(url, **kw):
        calls["n"] += 1
        raise RuntimeError(f"HTTP 504 from {url}")

    monkeypatch.setattr(media_lookup, "_get_json", always_504)

    # Three give-ups (threshold) trip the breaker.
    for _ in range(media_lookup._JIKAN_FAIL_THRESHOLD):
        assert media_lookup._jikan_get("https://api.jikan.moe/v4/anime?q=x") is None
    assert media_lookup.jikan_circuit_open()

    # Once open, further calls skip the network entirely.
    before = calls["n"]
    assert media_lookup._jikan_get("https://api.jikan.moe/v4/anime?q=y") is None
    assert calls["n"] == before  # no new request was made


def test_jikan_success_resets_failure_streak(monkeypatch):
    seq = ["HTTP 504", "HTTP 504", None]  # fail, fail, success

    def stepper(url, **kw):
        item = seq.pop(0)
        if item:
            raise RuntimeError(f"{item} from {url}")
        return {"data": []}

    # Each _jikan_get does its own retries; give it enough sequence to succeed.
    seq[:] = [None]
    monkeypatch.setattr(media_lookup, "_get_json", stepper)
    media_lookup._jikan_consecutive_failures = 2
    media_lookup._jikan_get("https://api.jikan.moe/v4/anime?q=x")
    assert media_lookup._jikan_consecutive_failures == 0


def test_plex_request_retries_once_on_timeout(monkeypatch):
    import plex_api

    monkeypatch.setattr(plex_api, "plex_metrics_enabled", lambda: True)
    monkeypatch.setattr(plex_api, "_normalized_base_url", lambda: "http://127.0.0.1:32400")
    monkeypatch.setattr(plex_api.config, "PLEX_REQUEST_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(plex_api.time, "sleep", lambda *_a: None)

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"MediaContainer": {"size": 1}}'

    calls = {"n": 0}

    def flaky_urlopen(req, timeout=None, context=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("timed out")
        return _Resp()

    monkeypatch.setattr(plex_api.urllib.request, "urlopen", flaky_urlopen)
    payload = plex_api._request_json("/library/sections")
    assert payload["MediaContainer"]["size"] == 1
    assert calls["n"] == 2  # first attempt timed out, retry succeeded


def test_plex_request_raises_clean_error_after_retries(monkeypatch):
    import plex_api

    monkeypatch.setattr(plex_api, "plex_metrics_enabled", lambda: True)
    monkeypatch.setattr(plex_api, "_normalized_base_url", lambda: "http://127.0.0.1:32400")
    monkeypatch.setattr(plex_api.time, "sleep", lambda *_a: None)
    monkeypatch.setattr(plex_api.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(TimeoutError("timed out")))
    with pytest.raises(RuntimeError, match="connection failed"):
        plex_api._request_json("/library/sections")
