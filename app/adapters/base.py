"""
Base classes for scanner adapters.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class CheckRecord:
    job_id: str
    source_id: str
    native_check_id: str
    canonical_check_id: Optional[str]
    status: str  # PASS|FAIL|WARNING|NA|UNKNOWN|ERROR|PENDING
    evidence: str  # JSON string
    observed_at: str  # ISO 8601 UTC


@dataclass
class AdapterResult:
    source_id: str
    status: str  # 'completed'|'failed'|'timeout'|'unavailable'
    raw_report: Optional[str]
    checks: List[CheckRecord]
    error: Optional[str] = None


class Adapter(ABC):
    source_id: str

    @abstractmethod
    async def scan(self, domain: str, job_id: str) -> AdapterResult:
        """Scan the domain and return AdapterResult containing CheckRecord list."""

    @abstractmethod
    async def health(self) -> str:
        """Returns 'ok'|'rate_limited'|'unavailable'."""
