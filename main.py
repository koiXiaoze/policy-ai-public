"""
政策情报系统 - 主入口，支持直接运行 python main.py
"""
import os
import sys
from pathlib import Path

# 必须在其他导入前加载 .env 文件到 os.environ
from dotenv import load_dotenv
env_path = Path(__file__).parent / ".env"
load_dotenv(env_path)
# 确保 TAVILY_API_KEY 写入 os.environ
os.environ.setdefault("TAVILY_API_KEY", "")

sys.path.insert(0, str(Path(__file__).parent))

from app.cli import main

if __name__ == "__main__":
    main()