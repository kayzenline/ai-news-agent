"""集中读取环境变量，避免密钥散落在代码里。"""
import os
from pathlib import Path


def _load_dotenv(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()

DATA_DIR = Path(os.getenv("DATA_DIR", "./data")).resolve()
# LLM_PROVIDER: grok | claude | openai | fake
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai").lower()
LLM_MODEL = os.getenv("LLM_MODEL", "")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")          # 通用 OpenAI 兼容（OpenAI/DeepSeek/Kimi）
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
XAI_API_KEY = os.getenv("XAI_API_KEY", "")
XAI_BASE_URL = os.getenv("XAI_BASE_URL", "https://api.x.ai/v1")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
MAX_STEPS = int(os.getenv("AGENT_MAX_STEPS", "15"))
MAX_TOOL_CALLS = int(os.getenv("AGENT_MAX_TOOL_CALLS", "30"))       # 每次运行总工具调用上限（含重复与修订）
RUN_DEADLINE_S = float(os.getenv("AGENT_RUN_DEADLINE_S", "180"))    # 软期限：每轮/每次工具调用前检查，不能中断进行中的同步调用
DEFAULT_TIMEZONE = os.getenv("DEFAULT_TIMEZONE", "Asia/Shanghai")  # 新用户默认 IANA 时区
CATCHUP_HOURS = float(os.getenv("CATCHUP_HOURS", "6"))  # 错过的定时任务在此窗口内补跑一次，超窗不补
MAX_CONCURRENT_RUNS = int(os.getenv("MAX_CONCURRENT_RUNS", "2"))  # 全局并发（手动+定时共用）
MAX_QUEUED_RUNS = int(os.getenv("MAX_QUEUED_RUNS", "10"))          # 排队上限，满了明确拒绝
TOOL_OUTPUT_LIMIT = 6000  # 单次工具输出截断，控制 token

SMTP = {
    "enabled": os.getenv("SMTP_ENABLED", "0") == "1",
    "host": os.getenv("SMTP_HOST", ""),
    "port": int(os.getenv("SMTP_PORT", "465")),
    "user": os.getenv("SMTP_USER", ""),
    "password": os.getenv("SMTP_PASSWORD", ""),
    "from": os.getenv("SMTP_FROM", ""),
}
