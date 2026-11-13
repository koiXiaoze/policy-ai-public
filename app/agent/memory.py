"""
Agent 记忆层
管理对话历史、上下文、长期记忆
"""

from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field
from datetime import datetime
import json
import os
from pathlib import Path


@dataclass
class MemoryEntry:
    """单条记忆记录"""
    role: str  # user / assistant / tool
    content: str
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: Dict[str, Any] = field(default_factory=dict)


class Memory:
    """Agent 记忆管理"""
    
    def __init__(self, max_short_term: int = 20, persist_path: str = None):
        self.max_short_term = max_short_term
        self.persist_path = persist_path or "data/agent_memory.json"
        self.short_term: List[MemoryEntry] = []
        self.long_term: List[Dict[str, Any]] = []
        self._load()
    
    def add(self, role: str, content: str, metadata: Dict[str, Any] = None):
        """添加记忆"""
        entry = MemoryEntry(role=role, content=content, metadata=metadata or {})
        self.short_term.append(entry)
        # 保持短期记忆大小
        if len(self.short_term) > self.max_short_term:
            # 将最旧的移入长期记忆
            old = self.short_term.pop(0)
            self.long_term.append({
                "role": old.role,
                "content": old.content,
                "timestamp": old.timestamp.isoformat(),
                "metadata": old.metadata
            })
        self._save()
    
    def get_recent(self, n: int = 10) -> List[MemoryEntry]:
        """获取最近 N 条短期记忆"""
        return self.short_term[-n:]
    
    def get_context(self, max_tokens: int = 4000) -> List[Dict[str, str]]:
        """获取用于 LLM 的上下文（OpenAI messages 格式）"""
        messages = []
        # 优先使用短期记忆
        for entry in reversed(self.short_term):
            messages.insert(0, {"role": entry.role, "content": entry.content})
            # 简单的 token 估算
            total_chars = sum(len(m["content"]) for m in messages)
            if total_chars > max_tokens * 3:  # 粗略估算
                break
        return messages
    
    def search_long_term(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """搜索长期记忆（简单关键词匹配）"""
        query_lower = query.lower()
        results = []
        for item in self.long_term:
            if query_lower in item.get("content", "").lower():
                results.append(item)
                if len(results) >= limit:
                    break
        return results
    
    def clear_short_term(self):
        """清空短期记忆"""
        self.short_term.clear()
        self._save()
    
    def _save(self):
        """持久化到文件"""
        try:
            os.makedirs(os.path.dirname(self.persist_path), exist_ok=True)
            data = {
                "short_term": [
                    {
                        "role": e.role,
                        "content": e.content,
                        "timestamp": e.timestamp.isoformat(),
                        "metadata": e.metadata
                    }
                    for e in self.short_term
                ],
                "long_term": self.long_term
            }
            with open(self.persist_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"记忆保存失败: {e}")
    
    def _load(self):
        """从文件加载"""
        try:
            if os.path.exists(self.persist_path):
                with open(self.persist_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.short_term = [
                    MemoryEntry(
                        role=e["role"],
                        content=e["content"],
                        timestamp=datetime.fromisoformat(e["timestamp"]),
                        metadata=e.get("metadata", {})
                    )
                    for e in data.get("short_term", [])
                ]
                self.long_term = data.get("long_term", [])
        except Exception as e:
            print(f"记忆加载失败: {e}")


# 全局记忆实例
memory = Memory()

print("Memory initialized")
