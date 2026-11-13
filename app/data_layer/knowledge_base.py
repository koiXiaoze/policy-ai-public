"""
Policy Knowledge Base
向量检索 + 关键词检索 混合搜索
"""

import os
import json
import hashlib
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
from datetime import datetime

from app.data_layer.source import PolicyItem, SourceType
from app.logger import logger


@dataclass
class KnowledgeEntry:
    """知识库条目"""
    id: str
    title: str
    content: str
    summary: str
    source_name: str
    government_level: str
    url: str
    publish_time: Optional[datetime]
    keywords: List[str]
    category: str
    embedding: Optional[List[float]] = None
    metadata: Dict[str, Any] = None


class PolicyKnowledgeBase:
    """政策知识库：支持关键词+向量混合检索"""
    
    def __init__(self, persist_dir: str = "data/knowledge_base"):
        self.persist_dir = persist_dir
        self.entries: List[KnowledgeEntry] = []
        self._keyword_index: Dict[str, List[str]] = {}  # keyword -> entry_ids
        self._use_vector = False
        self._vector_client = None
        self._init_vector()
        self._load()
    
    def _init_vector(self):
        """初始化向量数据库"""
        try:
            import chromadb
            self._vector_client = chromadb.PersistentClient(path=self.persist_dir)
            self._collection = self._vector_client.get_or_create_collection(
                name="policies",
                metadata={"hnsw:space": "cosine"}
            )
            self._use_vector = True
            logger.info("Vector database (ChromaDB) initialized")
        except ImportError:
            logger.info("ChromaDB not installed, using keyword-only search")
            self._use_vector = False
        except Exception as e:
            logger.warning(f"Vector DB init failed: {e}, using keyword-only")
            self._use_vector = False
    
    def _get_embedding(self, text: str) -> Optional[List[float]]:
        """获取文本嵌入向量"""
        if not self._use_vector:
            return None
        try:
            # 使用简单的hash作为伪嵌入（生产环境应使用真实embedding model）
            import hashlib
            hash_obj = hashlib.md5(text.encode())
            return [float(b) / 255.0 for b in hash_obj.digest()[:32]]
        except Exception:
            return None
    
    def add_policy(self, item: PolicyItem) -> str:
        """添加政策到知识库"""
        # 生成唯一ID
        entry_id = hashlib.md5(item.url.encode()).hexdigest()[:16]
        
        # 提取关键词
        keywords = self._extract_keywords(item.title + " " + (item.content or ""))
        
        entry = KnowledgeEntry(
            id=entry_id,
            title=item.title,
            content=item.content or item.summary or "",
            summary=item.summary or (item.content[:200] if item.content else ""),
            source_name=item.source_name,
            government_level=item.government_level,
            url=item.url,
            publish_time=item.publish_time,
            keywords=keywords,
            category=self._classify_category(item),
            metadata={
                "source_type": item.source_type.value,
                "importance_score": item.importance_score,
                "raw_data": item.raw_data
            }
        )
        
        # 生成向量
        if self._use_vector:
            entry.embedding = self._get_embedding(entry.title + " " + entry.content)
        
        # 存储
        self.entries.append(entry)
        self._update_keyword_index(entry)
        self._persist_entry(entry)
        
        return entry_id
    
    def _extract_keywords(self, text: str) -> List[str]:
        """简单关键词提取"""
        import re
        # 预定义的重要领域词
        domain_terms = [
            "人工智能", "数字经济", "新能源", "先进制造", "网络安全",
            "数据安全", "低空经济", "机器人", "生物医药", "新材料",
            "教育", "就业", "住房", "医疗", "养老", "人才", "社保",
            "税收", "金融", "贸易", "投资", "招商", "产业园", "高新区"
        ]
        found = []
        for term in domain_terms:
            if term in text:
                found.append(term)
        return found
    
    def _classify_category(self, item: PolicyItem) -> str:
        """分类政策类型"""
        text = (item.title + " " + (item.content or "")).lower()
        if any(kw in text for kw in ["人工智能", "AI", "数字经济", "算力", "大模型"]):
            return "人工智能"
        elif any(kw in text for kw in ["新能源", "光伏", "风电", "储能", "氢能"]):
            return "新能源"
        elif any(kw in text for kw in ["先进制造", "智能制造", "工业互联网", "工业母机"]):
            return "先进制造"
        elif any(kw in text for kw in ["就业", "招聘", "毕业生", "高校", "大学生"]):
            return "就业人才"
        elif any(kw in text for kw in ["住房", "房地产", "公积金", "租赁"]):
            return "住房"
        elif any(kw in text for kw in ["医疗", "医药", "卫生", "健康"]):
            return "医疗健康"
        elif any(kw in text for kw in ["税收", "财政", "金融", "银行", "保险"]):
            return "财税金融"
        return "综合其他"
    
    def _update_keyword_index(self, entry: KnowledgeEntry):
        for kw in entry.keywords:
            if kw not in self._keyword_index:
                self._keyword_index[kw] = []
            if entry.id not in self._keyword_index[kw]:
                self._keyword_index[kw].append(entry.id)
    
    def _persist_entry(self, entry: KnowledgeEntry):
        """持久化单条记录"""
        try:
            os.makedirs(self.persist_dir, exist_ok=True)
            file_path = os.path.join(self.persist_dir, f"{entry.id}.json")
            data = {
                "id": entry.id,
                "title": entry.title,
                "content": entry.content,
                "summary": entry.summary,
                "source_name": entry.source_name,
                "government_level": entry.government_level,
                "url": entry.url,
                "publish_time": entry.publish_time.isoformat() if entry.publish_time else None,
                "keywords": entry.keywords,
                "category": entry.category,
                "metadata": entry.metadata
            }
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Persist entry failed: {e}")
    
    def _load(self):
        """加载知识库"""
        try:
            if not os.path.exists(self.persist_dir):
                return
            
            for fname in os.listdir(self.persist_dir):
                if fname.endswith(".json"):
                    try:
                        with open(os.path.join(self.persist_dir, fname), "r", encoding="utf-8") as f:
                            data = json.load(f)
                        entry = KnowledgeEntry(
                            id=data["id"],
                            title=data["title"],
                            content=data["content"],
                            summary=data["summary"],
                            source_name=data["source_name"],
                            government_level=data["government_level"],
                            url=data["url"],
                            publish_time=datetime.fromisoformat(data["publish_time"]) if data.get("publish_time") else None,
                            keywords=data["keywords"],
                            category=data["category"],
                            metadata=data.get("metadata", {})
                        )
                        self.entries.append(entry)
                        self._update_keyword_index(entry)
                    except Exception as e:
                        logger.debug(f"Load entry failed: {e}")
            
            logger.info(f"Knowledge base loaded: {len(self.entries)} entries")
        except Exception as e:
            logger.warning(f"Knowledge base load failed: {e}")
    
    def search(self, query: str, limit: int = 10) -> List[KnowledgeEntry]:
        """混合检索：关键词 + 向量"""
        # 1. 关键词检索
        keyword_results = self._keyword_search(query, limit * 2)
        
        # 2. 向量检索（如果可用）
        vector_results = []
        if self._use_vector:
            vector_results = self._vector_search(query, limit * 2)
        
        # 3. 融合去重
        return self._fuse_results(keyword_results, vector_results, limit)
    
    def _keyword_search(self, query: str, limit: int) -> List[KnowledgeEntry]:
        query_lower = query.lower()
        scored = []
        
        for entry in self.entries:
            score = 0
            # 标题匹配
            if query_lower in entry.title.lower():
                score += 10
            # 关键词匹配
            for kw in entry.keywords:
                if kw.lower() in query_lower:
                    score += 5
            # 内容匹配
            if query_lower in entry.content.lower():
                score += 3
            # 摘要匹配
            if query_lower in entry.summary.lower():
                score += 2
            
            if score > 0:
                scored.append((score, entry))
        
        scored.sort(key=lambda x: x[0], reverse=True)
        return [e for _, e in scored[:limit]]
    
    def _vector_search(self, query: str, limit: int) -> List[KnowledgeEntry]:
        if not self._use_vector or not self._vector_client:
            return []
        
        try:
            query_embedding = self._get_embedding(query)
            if not query_embedding:
                return []
            
            results = self._collection.query(
                query_embeddings=[query_embedding],
                n_results=limit
            )
            
            entries = []
            for i, doc_id in enumerate(results.get("ids", [[]])[0]):
                for entry in self.entries:
                    if entry.id == doc_id:
                        entries.append(entry)
                        break
            return entries
        except Exception as e:
            logger.warning(f"Vector search failed: {e}")
            return []
    
    def _fuse_results(self, keyword_results: List[KnowledgeEntry], 
                      vector_results: List[KnowledgeEntry], limit: int) -> List[KnowledgeEntry]:
        """融合结果：RRF (Reciprocal Rank Fusion)"""
        all_entries = {}
        
        # 关键词结果权重
        for rank, entry in enumerate(keyword_results):
            all_entries[entry.id] = all_entries.get(entry.id, 0) + 1.0 / (rank + 1 + 60)
        
        # 向量结果权重
        for rank, entry in enumerate(vector_results):
            all_entries[entry.id] = all_entries.get(entry.id, 0) + 1.0 / (rank + 1 + 60)
        
        # 排序
        sorted_ids = sorted(all_entries.keys(), key=lambda x: all_entries[x], reverse=True)
        
        result = []
        for eid in sorted_ids[:limit]:
            for entry in self.entries:
                if entry.id == eid:
                    result.append(entry)
                    break
        
        return result
    
    def get_stats(self) -> Dict[str, Any]:
        return {
            "total_entries": len(self.entries),
            "categories": list(set(e.category for e in self.entries)),
            "sources": list(set(e.source_name for e in self.entries)),
            "vector_enabled": self._use_vector,
            "keyword_index_size": len(self._keyword_index)
        }


# 全局知识库实例
knowledge_base = PolicyKnowledgeBase()

print("PolicyKnowledgeBase initialized")
