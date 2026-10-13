"""一次真实模型配置检查：验证鉴权、接口与工具调用，不搜新闻、不发邮件。"""

import json

from .. import config
from .providers import get_provider

CHECK_TOOL = {
    "type": "function",
    "function": {
        "name": "configuration_check",
        "description": "Call this function once to confirm tool calling works.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


def main() -> int:
    if config.LLM_PROVIDER == "fake":
        print("当前是 fake 演示模式。请将 .env 的 LLM_PROVIDER 改为真实提供方后再检查。")
        return 2
    try:
        provider = get_provider()
    except ValueError as exc:
        print(f"配置未就绪：{exc}")
        return 2
    except Exception:
        print("无法初始化模型客户端，请检查 .env 与 SDK 安装。")
        return 2

    # 检查只发一次请求，不用完整 Agent 的超时/重试预算。
    client = provider.client.with_options(timeout=20, max_retries=0)
    provider.client = client
    try:
        response = provider.chat(
            [
                {
                    "role": "system",
                    "content": "You are testing tool support. Call configuration_check exactly once with {}. Do not reply with text.",
                },
                {"role": "user", "content": "Check tool calling now."},
            ],
            [CHECK_TOOL],
        )
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        messages = {
            400: "接口拒绝请求参数，请确认模型支持 Chat Completions 和 tools 等参数。",
            401: "鉴权失败，请检查 .env 的 API key 是否有效。",
            403: "没有调用权限，请检查账户或模型授权。",
            404: "接口或模型不存在，请检查 base_url 和模型名。",
            429: "额度不足或触发限流，请检查服务商账户状态。",
        }
        if status is not None:
            print(f"检查失败（HTTP {status}）：{messages.get(status, '服务商返回错误，请稍后重试。')}")
        elif "Timeout" in type(exc).__name__:
            print("检查失败：模型请求超时，请检查网络和服务商响应。")
        else:
            # 不回显 SDK 异常/响应正文，避免兼容服务将密钥带进错误信息。
            print("检查失败：网络连接或响应解析异常，请检查 API 地址、代理和服务状态。")
        return 1
    finally:
        client.close()

    calls = response.tool_calls
    try:
        supported = len(calls) == 1 and calls[0].name == "configuration_check" and json.loads(calls[0].arguments) == {}
    except (ValueError, TypeError):
        supported = False
    if not supported:
        print("请求成功，但未返回预期工具调用，尚不能确认此模型适用于新闻 Agent。")
        return 1
    print("配置检查通过：鉴权、接口响应和工具调用正常。")
    print(f"本次 token：输入 {response.usage.get('input', 0)}，输出 {response.usage.get('output', 0)}。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
