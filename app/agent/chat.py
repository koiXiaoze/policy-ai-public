#!/usr/bin/env python3
"""
Policy Intelligence Agent - 命令行交互界面
使用方法：python -m app.agent.chat
"""

import sys
from pathlib import Path

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from app.agent import policy_agent
from app.logger import logger


def print_banner():
    print("=" * 60)
    print("  Policy Intelligence Agent - 政策智能分析助手")
    print("=" * 60)
    print()
    print("输入自然语言查询，例如：")
    print("  - 今天国务院有什么重要政策？")
    print("  - 分析湖北最近一个月人工智能产业政策")
    print("  - 武汉最近半年有哪些影响大学生就业的政策")
    print("  - 采集最新政策")
    print()
    print("输入 'quit' 或 'exit' 退出")
    print("输入 'help' 查看帮助")
    print("-" * 60)


def print_help():
    print("\n可用命令：")
    print("  help     - 显示帮助")
    print("  quit     - 退出程序")
    print("  clear    - 清空对话历史")
    print("  debug    - 显示最后一次执行的详细状态")
    print("\n查询示例：")
    print("  - 今天国务院发布了什么政策？")
    print("  - 湖北省最近有什么人工智能政策？")
    print("  - 分析武汉最近半年新能源产业政策趋势")
    print("  - 十堰有哪些汽车产业相关政策？")
    print("  - 最近一个月人社部有什么就业政策？")
    print("  - 帮我生成今日政策日报")
    print("  - 采集最新政策")


def main():
    print_banner()
    
    while True:
        try:
            user_input = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\n再见！")
            break
        
        if not user_input:
            continue
        
        if user_input.lower() in ("quit", "exit", "q"):
            print("再见！")
            break
        
        if user_input.lower() == "help":
            print_help()
            continue
        
        if user_input.lower() == "clear":
            from app.agent.memory import memory
            memory.clear_short_term()
            print("对话历史已清空")
            continue
        
        if user_input.lower() == "debug":
            # 显示最后一次执行状态
            from app.agent.memory import memory
            recent = memory.get_recent(5)
            print("\n最近对话历史：")
            for entry in recent:
                print(f"  [{entry.role}] {entry.content[:100]}...")
            continue
        
        # 执行 Agent
        print("\n正在分析...")
        result = policy_agent.run(user_input)
        
        if result.success:
            print(f"\n{result.answer}")
        else:
            print(f"\n❌ 执行失败: {result.error}")
            if result.state and result.state.tool_calls:
                print("\n执行步骤：")
                for tc in result.state.tool_calls:
                    status = "✓" if tc.status.value == "success" else "✗"
                    print(f"  {status} {tc.name}({tc.arguments})")
                    if tc.error:
                        print(f"     错误: {tc.error}")


if __name__ == "__main__":
    main()
