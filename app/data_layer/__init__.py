"""
Data Layer Package
"""

from app.data_layer.source import (
    SourceType,
    PolicyItem,
    PolicySource,
    LocalDBSource,
    WebSearchSource,
    OfficialAPISource,
    source_registry
)

from app.data_layer.router import (
    QueryType,
    RouteDecision,
    DataRouter,
    data_router
)

from app.data_layer.knowledge_base import (
    KnowledgeEntry,
    PolicyKnowledgeBase,
    knowledge_base
)

from app.data_layer.query_parser import (
    PolicyQuery,
    TimeRange,
    AuthorityLevel,
    PolicyQueryParser,
    query_parser,
    parse_policy_query
)

from app.data_layer.source_validator import (
    SourceAuthority,
    SourceScore,
    SourceValidator,
    source_validator
)

from app.data_layer.date_filter import (
    DateFilterResult,
    DateRangeFilter,
    date_filter
)

from app.data_layer.classifier import (
    PolicyType,
    ClassificationResult,
    PolicyTypeClassifier,
    policy_classifier
)

from app.data_layer.ranker import (
    RankingResult,
    ResultRanker,
    result_ranker
)

__all__ = [
    "SourceType",
    "PolicyItem",
    "PolicySource",
    "LocalDBSource",
    "WebSearchSource",
    "OfficialAPISource",
    "source_registry",
    "QueryType",
    "RouteDecision",
    "DataRouter",
    "data_router",
    "KnowledgeEntry",
    "PolicyKnowledgeBase",
    "knowledge_base",
    "PolicyQuery",
    "TimeRange",
    "AuthorityLevel",
    "PolicyQueryParser",
    "query_parser",
    "parse_policy_query",
    "SourceAuthority",
    "SourceScore",
    "SourceValidator",
    "source_validator",
    "DateFilterResult",
    "DateRangeFilter",
    "date_filter",
    "PolicyType",
    "ClassificationResult",
    "PolicyTypeClassifier",
    "policy_classifier",
    "RankingResult",
    "ResultRanker",
    "result_ranker",
]

print("Data layer package initialized")
