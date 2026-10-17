import os
import sys

# 让 tests/ 能 import 到项目根目录下的代理模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
