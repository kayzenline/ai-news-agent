import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import config
from app.llm.providers import FakeProvider, get_provider


@pytest.fixture
def sdk_constructors(monkeypatch):
    """只检查 SDK 构造参数，不使用网络或真实密钥。"""
    openai = Mock()
    anthropic = Mock()
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=openai))
    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=anthropic))
    return openai, anthropic


@pytest.mark.parametrize("provider,variable", [
    ("openai", "LLM_API_KEY"),
    ("grok", "XAI_API_KEY"),
    ("claude", "ANTHROPIC_API_KEY"),
])
@pytest.mark.parametrize("key", ["", " \t\n "])
def test_missing_api_key_explains_local_configuration(monkeypatch, sdk_constructors, provider, variable, key):
    monkeypatch.setattr(config, "LLM_PROVIDER", provider)
    monkeypatch.setattr(config, variable, key)

    with pytest.raises(ValueError) as error:
        get_provider()

    message = str(error.value)
    assert variable in message
    assert ".env" in message
    assert "重启" in message
    for constructor in sdk_constructors:
        constructor.assert_not_called()


def test_fake_needs_no_credentials_or_url(monkeypatch, sdk_constructors):
    monkeypatch.setattr(config, "LLM_PROVIDER", "fake")
    for variable in ("LLM_API_KEY", "XAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setattr(config, variable, "")
    monkeypatch.setattr(config, "LLM_BASE_URL", "")
    monkeypatch.setattr(config, "XAI_BASE_URL", "")

    assert isinstance(get_provider(), FakeProvider)
    for constructor in sdk_constructors:
        constructor.assert_not_called()


@pytest.mark.parametrize("provider,key_variable,url_variable", [
    ("openai", "LLM_API_KEY", "LLM_BASE_URL"),
    ("grok", "XAI_API_KEY", "XAI_BASE_URL"),
])
@pytest.mark.parametrize("url", ["https://compat.example/v1", "http://localhost:1234/v1", "http://[::1]:1234/v1"])
def test_compatible_service_preserves_explicit_model(monkeypatch, sdk_constructors, provider, key_variable, url_variable, url):
    monkeypatch.setattr(config, "LLM_PROVIDER", provider)
    monkeypatch.setattr(config, key_variable, "arbitrary-test-key")
    monkeypatch.setattr(config, url_variable, url)
    monkeypatch.setattr(config, "LLM_MODEL", "grok-4")

    result = get_provider()

    assert result.name == provider
    assert result.model == "grok-4"
    sdk_constructors[0].assert_called_once_with(api_key="arbitrary-test-key", base_url=url, timeout=60, max_retries=2)
    sdk_constructors[1].assert_not_called()


@pytest.mark.parametrize("provider,key_variable,url_variable", [
    ("openai", "LLM_API_KEY", "LLM_BASE_URL"),
    ("grok", "XAI_API_KEY", "XAI_BASE_URL"),
])
@pytest.mark.parametrize("url", [
    "", "   ", "ftp://compat.example/v1", "compat.example/v1", "https:///v1",
    "https://user:private-password@compat.example/v1", "http://localhost:abc/v1", "http://[",
])
def test_invalid_base_url_fails_before_sdk_without_echoing_credentials(monkeypatch, sdk_constructors, provider, key_variable, url_variable, url):
    monkeypatch.setattr(config, "LLM_PROVIDER", provider)
    monkeypatch.setattr(config, key_variable, "private-api-key")
    monkeypatch.setattr(config, url_variable, url)

    with pytest.raises(ValueError) as error:
        get_provider()

    message = str(error.value)
    assert url_variable in message
    assert ".env" in message
    assert "重启" in message
    assert "private-password" not in message
    assert "private-api-key" not in message
    for constructor in sdk_constructors:
        constructor.assert_not_called()


def test_claude_configuration_is_passed_to_sdk(monkeypatch, sdk_constructors):
    monkeypatch.setattr(config, "LLM_PROVIDER", "claude")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "arbitrary-test-key")
    monkeypatch.setattr(config, "LLM_MODEL", "custom-model")

    result = get_provider()

    assert result.model == "custom-model"
    sdk_constructors[1].assert_called_once_with(api_key="arbitrary-test-key", timeout=60, max_retries=2)
    sdk_constructors[0].assert_not_called()


@pytest.mark.parametrize("url,expected", [
    ("https://api.deepseek.com", {"extra_body": {"thinking": {"type": "disabled"}}}),
    ("https://api.deepseek.com/v1", {"extra_body": {"thinking": {"type": "disabled"}}}),
    ("https://api.openai.com/v1", {}),
    ("https://compat.example/v1", {}),
])
def test_deepseek_non_thinking_option_is_scoped_to_official_endpoint(monkeypatch, sdk_constructors, url, expected):
    monkeypatch.setattr(config, "LLM_PROVIDER", "openai")
    monkeypatch.setattr(config, "LLM_API_KEY", "arbitrary-test-key")
    monkeypatch.setattr(config, "LLM_BASE_URL", url)
    monkeypatch.setattr(config, "LLM_MODEL", "deepseek-flash")
    provider = get_provider()
    create = provider.client.chat.completions.create
    create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="OK", tool_calls=[]))],
        usage=SimpleNamespace(prompt_tokens=3, completion_tokens=1))

    result = provider.chat([{"role": "user", "content": "Hello"}], [])

    assert result.text == "OK"
    assert result.usage == {"input": 3, "output": 1}
    kwargs = create.call_args.kwargs
    assert kwargs["model"] == "deepseek-flash"
    assert {k: kwargs[k] for k in ("extra_body",) if k in kwargs} == expected
