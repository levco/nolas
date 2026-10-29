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
