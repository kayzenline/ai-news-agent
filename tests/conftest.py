import os
import pathlib
import sys
import warnings

warnings.filterwarnings("ignore", category=ResourceWarning)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

# 测试中后台补跑扫描不应因“当前真实时间”触发运行；需要补跑的用例自己设置窗口
os.environ.setdefault("CATCHUP_HOURS", "0")
