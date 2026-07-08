"""Company data models for deduplication (HubSpot Companies / org records)."""
from __future__ import annotations
from pydantic import BaseModel
from typing import Optional, Any
from datetime import datetime
import re

# Trailing legal/entity suffixes stripped when normalizing a company name, so
# "Acme, Inc." and "Acme LLC" compare equal.
_LEGAL_SUFFIXES = {
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation",
    "co", "company", "gmbh", "ag", "sa", "sas", "srl", "bv", "nv", "plc",
    "pvt", "pte", "llp", "lp", "group", "holdings", "holding",
    "international", "intl",
}


class Company(BaseModel):
    """Represents a CRM company/organization record."""
    id: str
    name: Optional[str] = None
    domain: Optional[str] = None
    website: Optional[str] = None
    phone: Optional[str] = None
    industry: Optional[str] = None
    country: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    association_count: int = 0  # Associated contacts, deals, etc.
    raw_properties: dict[str, Any] = {}

    @property
    def normalized_domain(self) -> Optional[str]:
        """Bare domain (no scheme/www/path), lowercased — the strongest company
        dedupe key. Uses `domain`, falling back to `website`."""
        raw = self.domain or self.website
        if not raw:
            return None
        d = raw.strip().lower()
        d = re.sub(r"^https?://", "", d)
        d = re.sub(r"^www\.", "", d)
        d = d.split("/")[0].split("?")[0].strip()
        return d or None

    @property
    def normalized_name(self) -> str:
        """Lowercased company name with punctuation and trailing legal suffixes
        stripped, so 'Acme, Inc.' == 'Acme LLC'."""
        if not self.name:
            return ""
        n = re.sub(r"[^\w\s]", " ", self.name.lower())
        tokens = [t for t in n.split() if t]
        if tokens and tokens[0] == "the":
            tokens = tokens[1:]
        while tokens and tokens[-1] in _LEGAL_SUFFIXES:
            tokens.pop()
        return " ".join(tokens).strip()

    @property
    def name_prefix(self) -> Optional[str]:
        """First 3 chars of the normalized name, for blocking."""
        n = self.normalized_name
        return n[:3] if n else None
