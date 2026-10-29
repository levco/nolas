import pytest

from settings.settings import Settings


def test_thread_index_defaults_to_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("USE_THREAD_INDEX", raising=False)
    config = Settings(ENVIRONMENT="test", PASSWORD_ENCRYPTION_KEY="test", _env_file=None)
    assert config.use_thread_index is True


def test_thread_index_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_THREAD_INDEX", "false")
    config = Settings(ENVIRONMENT="test", PASSWORD_ENCRYPTION_KEY="test", _env_file=None)
    assert config.use_thread_index is False


@pytest.mark.parametrize("environment", ["development", "staging", "qa", "production", "test", "unit_test"])
@pytest.mark.parametrize("use_sentinel", [None, "true", "false"])
def test_sentinel_selection(monkeypatch: pytest.MonkeyPatch, environment: str, use_sentinel: str | None) -> None:
    monkeypatch.delenv("USE_SENTINEL", raising=False)
    if use_sentinel is not None:
        monkeypatch.setenv("USE_SENTINEL", use_sentinel)
    config = Settings(ENVIRONMENT=environment, PASSWORD_ENCRYPTION_KEY="test", _env_file=None)
    assert config.use_sentinel is (use_sentinel != "false" and environment not in ("test", "unit_test"))


def test_sentinel_settings_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "SENTINEL_HOST": "redis-sentinel",
        "SENTINEL_PORT": "26379",
        "SENTINEL_MASTER_NAME": "nolas-master",
        "SENTINEL_SOCKET_TIMEOUT": "5",
        "SENTINEL_PASSWORD": "sentinel-secret",
        "REDIS_PASSWORD": "redis-secret",
    }.items():
        monkeypatch.setenv(name, value)
    config = Settings(ENVIRONMENT="production", PASSWORD_ENCRYPTION_KEY="test", _env_file=None)
    assert config.redis.sentinel_host == "redis-sentinel"
    assert config.redis.sentinel_port == 26379
    assert config.redis.sentinel_master_name == "nolas-master"
    assert config.redis.sentinel_socket_timeout == 5
    assert config.redis.sentinel_password == "sentinel-secret"
    assert config.redis.redis_password == "redis-secret"
