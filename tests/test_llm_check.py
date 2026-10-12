from types import SimpleNamespace

import pytest

from app.llm import check
from app.llm.base import LLMResponse, ToolCall


def test_fake_check_does_not_send_a_request(monkeypatch, capsys):
    monkeypatch.setattr(check.config, "LLM_PROVIDER", "fake")
    monkeypatch.setattr(check, "get_provider", lambda: pytest.fail("must not initialize a client"))
    assert check.main() == 2
    assert "fake" in capsys.readouterr().out


def test_check_requires_valid_tool_call_and_limits_retries(monkeypatch, capsys):
    monkeypatch.setattr(check.config, "LLM_PROVIDER", "openai")
    options, closed = [], []
    client = SimpleNamespace(close=lambda: closed.append(True))
    client.with_options = lambda **kw: options.append(kw) or client

    def chat(messages, tools):
        assert tools == [check.CHECK_TOOL]
        return LLMResponse("", [ToolCall("c1", "configuration_check", "{}")], "tool_use")

    monkeypatch.setattr(check, "get_provider", lambda: SimpleNamespace(client=client, chat=chat))
    assert check.main() == 0
    assert options == [{"timeout": 20, "max_retries": 0}]
    assert closed == [True]
    assert "检查通过" in capsys.readouterr().out


def test_check_hides_provider_error_body(monkeypatch, capsys):
    monkeypatch.setattr(check.config, "LLM_PROVIDER", "openai")
    client = SimpleNamespace(close=lambda: None)
    client.with_options = lambda **kw: client

    def chat(*args):
        error = RuntimeError("secret-provider-response")
        error.status_code = 401
        raise error

    monkeypatch.setattr(check, "get_provider", lambda: SimpleNamespace(client=client, chat=chat))
    assert check.main() == 1
    output = capsys.readouterr().out
    assert "401" in output and "secret-provider-response" not in output


def test_plain_text_reply_does_not_pass_tool_support_check(monkeypatch, capsys):
    monkeypatch.setattr(check.config, "LLM_PROVIDER", "openai")
    client = SimpleNamespace(close=lambda: None)
    client.with_options = lambda **kw: client
    monkeypatch.setattr(check, "get_provider", lambda: SimpleNamespace(
        client=client, chat=lambda *a: LLMResponse("OK", [], "end")))
    assert check.main() == 1
    assert "未返回预期工具调用" in capsys.readouterr().out
