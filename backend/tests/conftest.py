import sys
from pathlib import Path

# 让测试可以直接 import backend/ 下的模块（safety.py 等）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
