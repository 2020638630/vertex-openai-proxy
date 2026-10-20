import os
import sys
import tempfile

# 测试不打真实网络、不写生产日志、不污染真实用量账本
# （必须在 import 代理模块之前设置）
os.environ["LOG_TO_FILE"] = "0"
os.environ["PREFLIGHT"] = "0"
os.environ["USAGE_DIR"] = tempfile.mkdtemp(prefix="proxy-usage-test-")

# 让 tests/ 能 import 到项目根目录下的代理模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
