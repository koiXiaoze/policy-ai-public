
"""
日志配置模块
统一管理项目日志输出
"""

import logging
import sys
import io
from pathlib import Path
from app.config import settings


def setup_logger(name: str = "pia") -> logging.Logger:
    """配置并返回日志记录器
    
    Args:
        name: 日志记录器名称

    Returns:
        配置好的 Logger 实例
    """
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))

    if logger.handlers:
        return logger

    # 控制台处理器 - Windows 下强制 UTF-8 编码，避免中文乱码
    # 使用 TextIOWrapper 包装 stdout.buffer，指定 encoding=utf-8
    console_stream = io.TextIOWrapper(
        sys.stdout.buffer,
        encoding="utf-8",
        errors="replace",
        write_through=True
    )
    console_handler = logging.StreamHandler(console_stream)
    console_handler.setLevel(logging.INFO)
    console_fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S"
    )
    console_handler.setFormatter(console_fmt)
    logger.addHandler(console_handler)

    # 文件处理器
    log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(
        log_dir / "pia.log",
        encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    file_handler.setFormatter(file_fmt)
    logger.addHandler(file_handler)

    return logger


# 默认日志记录器
logger = setup_logger()

