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
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
LLM_FAKE = os.getenv("LLM_FAKE", "0") == "1" or not LLM_API_KEY
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
MAX_STEPS = int(os.getenv("AGENT_MAX_STEPS", "15"))
TOOL_OUTPUT_LIMIT = 6000  # 单次工具输出截断，控制 token

SMTP = {
    "enabled": os.getenv("SMTP_ENABLED", "0") == "1",
    "host": os.getenv("SMTP_HOST", ""),
    "port": int(os.getenv("SMTP_PORT", "465")),
    "user": os.getenv("SMTP_USER", ""),
    "password": os.getenv("SMTP_PASSWORD", ""),
    "from": os.getenv("SMTP_FROM", ""),
}
