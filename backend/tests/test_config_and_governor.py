import pytest

from backend.app.config import Settings, UnsafeDataDirectory
from backend.app.kalshi import CircuitExhausted, CircuitOpen, RequestGovernor
from backend.app.storage import StorageLimitExceeded, Store


def test_refuses_data_inside_repository(monkeypatch, tmp_path):
    monkeypatch.setenv("KALSHI_DATA_DIR", str(tmp_path / "data"))
    with pytest.raises(UnsafeDataDirectory):
        Settings.from_environment(tmp_path)


def test_defaults_real_and_reads_circuit_configuration(monkeypatch, tmp_path):
    monkeypatch.delenv("KALSHI_SYNC_MODE", raising=False)
    monkeypatch.setenv("KALSHI_DATA_DIR", str(tmp_path.parent / "outside-data"))
    monkeypatch.setenv("KALSHI_BASE_URL", "https://example.test/api/")
    monkeypatch.setenv("KALSHI_429_PAUSE_SECONDS", "7")
    monkeypatch.setenv("KALSHI_429_MAX_PAUSES", "2")
    settings = Settings.from_environment(tmp_path)
    assert settings.sync_mode == "real"
    assert settings.base_url == "https://example.test/api"
    assert (settings.pause_seconds, settings.max_pauses) == (7, 2)


def test_repository_dotenv_loads_without_overriding_shell(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("KALSHI_SYNC_MODE=demo\nKALSHI_429_PAUSE_SECONDS=12\n")
    monkeypatch.delenv("KALSHI_SYNC_MODE", raising=False)
    monkeypatch.delenv("KALSHI_429_PAUSE_SECONDS", raising=False)
    monkeypatch.setenv("KALSHI_DATA_DIR", str(tmp_path.parent / "dotenv-data"))
    settings = Settings.from_environment(tmp_path)
    assert (settings.sync_mode, settings.pause_seconds) == ("demo", 12)
    monkeypatch.setenv("KALSHI_SYNC_MODE", "real")
    assert Settings.from_environment(tmp_path).sync_mode == "real"


def test_optional_storage_limit_is_unlimited_when_blank(monkeypatch, tmp_path):
    monkeypatch.setenv("KALSHI_DATA_DIR", str(tmp_path.parent / "storage-data"))
    monkeypatch.setenv("KALSHI_MAX_STORAGE_GB", "")
    assert Settings.from_environment(tmp_path).max_storage_bytes is None
    monkeypatch.setenv("KALSHI_MAX_STORAGE_GB", "1.5")
    assert Settings.from_environment(tmp_path).max_storage_bytes == int(1.5 * 1024**3)
    monkeypatch.setenv("KALSHI_MAX_STORAGE_GB", "0")
    with pytest.raises(ValueError, match="KALSHI_MAX_STORAGE_GB"):
        Settings.from_environment(tmp_path)


def test_store_refuses_writes_past_configured_storage_limit(tmp_path):
    store = Store(tmp_path / "data")
    store.max_storage_bytes = store.storage_bytes()
    with pytest.raises(StorageLimitExceeded, match="Local storage limit reached"):
        store.append_staged_catalog("limit-run", [{"ticker": "LIMIT", "title": "limit"}])
    store.close()


@pytest.mark.asyncio
async def test_circuit_opens_and_exhausts(monkeypatch):
    now = [0.0]
    governor = RequestGovernor(5, pause_seconds=7, max_pauses=3, clock=lambda: now[0])
    await governor.record_429()
    with pytest.raises(CircuitOpen):
        await governor.wait_until_allowed()
    assert await governor.seconds_remaining() == 7
    now[0] = 8
    await governor.wait_until_allowed()
    await governor.record_429()
    now[0] = 16
    await governor.wait_until_allowed()
    await governor.record_429()
    now[0] = 24
    await governor.wait_until_allowed()
    with pytest.raises(CircuitExhausted):
        await governor.record_429()
