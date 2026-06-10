"""
Config-driven, record-agnostic match engine (cross-client).

This is the new matching spine described in 06-Cross-Client-Build-Plan.md. It
operates on raw Salesforce record dicts (the same shape stored in
Contact.raw_properties and returned by `sf data query`), driven entirely by a
per-client MatchProfile so no client field name is ever hardcoded.

Pipeline (deterministic-first, probabilistic-fallback):
  PASS 0  deterministic keys  -> exact short-circuit, confidence 1.0
  PASS 1  blocking            -> candidate generation
  PASS 2  match modes         -> exact_fingerprint / fuzzy / picklist / exact
  PASS 3  discriminator veto  -> cannot-link constraints (negative edges)
  PASS 4  assembly            -> Union-Find with cannot-link + bridging guards
  POST    hierarchy + buckets -> ParentId classification, safe/needs_review

Dry-run only: this module never writes to Salesforce. It produces Clusters for
review. Merge execution is a separate, later concern.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


# --------------------------------------------------------------------------- #
# Normalizers (named, composable — referenced by name from the profile)
# --------------------------------------------------------------------------- #

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")
_LEGAL_SUFFIXES = {
    "inc", "incorporated", "llc", "llp", "lp", "ltd", "limited", "co", "company",
    "corp", "corporation", "gmbh", "ag", "sa", "sas", "sarl", "srl", "spa", "bv",
    "nv", "oy", "ab", "as", "plc", "pty", "pte", "kg", "kk", "kabushiki", "kaisha",
    "holding", "holdings", "group", "the",
}
_FREEMAIL = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com", "icloud.com",
    "gmx.com", "gmx.net", "mail.com", "protonmail.com", "yandex.com", "qq.com",
    "163.com", "live.com", "msn.com", "me.com",
}
_PLACEHOLDER_NAMES = {
    "", "unknown", "<unknown>", "test", "test account", "n/a", "na", "none", "null",
    "tbd", ".", "-", "do not use", "donotuse", "unknown account", "sample", "xxx",
}


def norm_lower(v: Any) -> str:
    return str(v or "").strip().lower()


def norm_picklist(v: Any) -> str:
    # Picklist / code values are already standardized — compare verbatim (trim only).
    return str(v or "").strip()


def norm_legal_name(v: Any) -> str:
    s = norm_lower(v)
    s = _PUNCT.sub(" ", s)
    tokens = [t for t in _WS.sub(" ", s).strip().split(" ") if t and t not in _LEGAL_SUFFIXES]
    return " ".join(tokens)


def norm_domain(v: Any) -> str:
    s = norm_lower(v)
    if not s:
        return ""
    s = re.sub(r"^\w+://", "", s)          # strip scheme
    s = s.split("/")[0].split("?")[0]       # strip path/query
    s = s.split("@")[-1]                     # strip any userinfo / take host of an email
    if s.startswith("www."):
        s = s[4:]
    return s.strip()


NORMALIZERS: dict[str, Callable[[Any], str]] = {
    "lower": norm_lower,
    "picklist": norm_picklist,
    "legal_name": norm_legal_name,
    "domain": norm_domain,
    "as_is": lambda v: str(v or "").strip(),
}


# --------------------------------------------------------------------------- #
# Profile (config-as-data) and Cluster (output)
# --------------------------------------------------------------------------- #

@dataclass
class MatchProfile:
    """A per-client matching profile. Loaded from JSON; bound to real API names."""
    object_type: str
    version: str
    fields: dict[str, dict]                 # role -> {api, normalizer}
    fingerprint: list[str]                  # roles forming the exact-match composite
    deterministic_keys: list[dict] = field(default_factory=list)   # [{role, require_nonblank}]
    discriminators: list[dict] = field(default_factory=list)       # [{role, blank_handling}]
    fuzzy_rules: list[dict] = field(default_factory=list)          # [{role, weight, normalizer}]
    threshold: float = 0.90
    blocking: list[list[str]] = field(default_factory=list)        # list of role-lists
    require_eligible: list[str] = field(default_factory=list)      # roles that must be non-blank
    filter_placeholder_names: list[str] = field(default_factory=list)  # roles to check
    filter_freemail_domains: list[str] = field(default_factory=list)   # roles (domains) to check
    parent_role: Optional[str] = None       # role holding ParentId
    id_role: str = "Id"
    activity_role: Optional[str] = None
    protect_role: Optional[str] = None
    verification: dict = field(default_factory=dict)  # auto_paths / require_safe_bucket / require_discriminators_conclusive

    @staticmethod
    def from_json(path: str) -> "MatchProfile":
        with open(path) as f:
            d = json.load(f)
        return MatchProfile(**d)

    # -- field access helpers ------------------------------------------------ #
    def _api(self, role: str) -> Optional[str]:
        spec = self.fields.get(role)
        return spec.get("api") if spec else None

    def _normalizer(self, role: str) -> Callable[[Any], str]:
        spec = self.fields.get(role, {})
        return NORMALIZERS[spec.get("normalizer", "as_is")]

    def value(self, record: dict, role: str) -> str:
        """Normalized value of a role for a record (empty string if blank/missing)."""
        api = self._api(role)
        if not api:
            return ""
        return self._normalizer(role)(record.get(api))

    def record_id(self, record: dict) -> str:
        return str(record.get(self.id_role) or record.get("Id"))


@dataclass
class Cluster:
    cluster_id: str
    member_ids: list[str]
    fingerprint: str
    match_path: str               # 'deterministic' | 'exact_fingerprint' | 'fuzzy' | 'mixed'
    confidence: float             # 0-1
    hierarchy_class: str          # 'disconnected_dupe' | 'sibling_dupe' | 'hierarchy_explained' | 'mixed'
    bucket: str                   # activity/protect safety: 'auto_safe' | 'needs_review' | 'known_active'
    certainty: str = "certain"            # match certainty: 'certain' | 'review'
    verification_status: str = "needs_verification"   # gate: 'auto_merge' | 'needs_verification'
    verification_reason: str = ""
    members: list[dict] = field(default_factory=list)   # display snapshots

    @property
    def is_dupe(self) -> bool:
        return self.hierarchy_class != "hierarchy_explained"


@dataclass
class MatchResult:
    clusters: list[Cluster]
    stats: dict[str, Any]


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #

class MatchEngine:
    """Runs a MatchProfile over a list of raw SF record dicts (dry-run)."""

    def __init__(self, profile: MatchProfile):
        self.p = profile

    # -- eligibility / false-positive filtering ------------------------------ #
    def _eligible(self, rec: dict) -> bool:
        p = self.p
        for role in p.require_eligible:
            if not p.value(rec, role):
                return False
        for role in p.filter_placeholder_names:
            if p.value(rec, role).lower() in _PLACEHOLDER_NAMES:
                return False
        for role in p.filter_freemail_domains:
            if p.value(rec, role) in _FREEMAIL:
                return False
        return True

    def _fingerprint(self, rec: dict) -> Optional[str]:
        parts = []
        for role in self.p.fingerprint:
            v = self.p.value(rec, role)
            if not v:
                return None                      # require all fingerprint components
            parts.append(v)
        return "|".join(parts)

    # -- discriminators ------------------------------------------------------ #
    def _violates_discriminator(self, a: dict, b: dict) -> bool:
        for disc in self.p.discriminators:
            role = disc["role"]
            va, vb = self.p.value(a, role), self.p.value(b, role)
            blank = disc.get("blank_handling", "skip")
            if not va or not vb:
                if blank == "veto":
                    return True
                continue                          # 'skip': blanks never veto
            if va != vb:
                return True
        return False

    # -- fuzzy scoring (lazy rapidfuzz) -------------------------------------- #
    def _fuzzy_score(self, a: dict, b: dict) -> float:
        from rapidfuzz import fuzz               # lazy: not needed for exact-fingerprint runs
        scores, weights = [], []
        for rule in self.p.fuzzy_rules:
            role = rule["role"]
            normr = NORMALIZERS[rule.get("normalizer", "legal_name")]
            va, vb = normr(a.get(self.p._api(role))), normr(b.get(self.p._api(role)))
            if va and vb:
                scores.append(fuzz.token_sort_ratio(va, vb) / 100.0)
                weights.append(rule.get("weight", 1.0))
        if not scores:
            return 0.0
        return sum(s * w for s, w in zip(scores, weights)) / sum(weights)

    # -- main ---------------------------------------------------------------- #
    def find_clusters(self, records: list[dict]) -> MatchResult:
        p = self.p
        by_id = {p.record_id(r): r for r in records}

        eligible, ineligible = {}, 0
        for rid, r in by_id.items():
            if self._eligible(r):
                eligible[rid] = r
            else:
                ineligible += 1

        # Union-Find ---------------------------------------------------------
        parent: dict[str, str] = {}

        def find(x):
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        members_of = lambda root: [i for i in parent if find(i) == root]

        cannot_link: set[frozenset] = set()
        det_cluster: dict[str, str] = {}        # record -> deterministic key-cluster tag
        edge_type: dict[frozenset, str] = {}    # matched pair -> 'deterministic'|'exact_fingerprint'|'fuzzy'

        def try_union(x, y) -> bool:
            rx, ry = find(x), find(y)
            if rx == ry:
                return True
            # cannot-link guard: never merge if it co-locates a vetoed pair
            merged = set(members_of(rx)) | set(members_of(ry))
            for pair in cannot_link:
                if pair <= merged:
                    return False
            # bridging guard: a fuzzy/fingerprint edge may not bridge two distinct
            # deterministic key-clusters
            dx = {det_cluster[m] for m in members_of(rx) if m in det_cluster}
            dy = {det_cluster[m] for m in members_of(ry) if m in det_cluster}
            if dx and dy and dx != dy:
                return False
            parent[ry] = rx
            return True

        for rid in eligible:
            parent.setdefault(rid, rid)

        # PASS 0 — deterministic keys (exact short-circuit, confidence 1.0)
        det_pairs = 0
        for key_spec in p.deterministic_keys:
            role = key_spec["role"]
            buckets: dict[str, list[str]] = defaultdict(list)
            for rid, r in eligible.items():
                v = p.value(r, role)
                if v or not key_spec.get("require_nonblank", True):
                    if v:
                        buckets[v].append(rid)
            for v, ids in buckets.items():
                if len(ids) < 2:
                    continue
                tag = f"det:{role}:{v}"
                for m in ids:
                    det_cluster[m] = tag
                for m in ids[1:]:
                    if try_union(ids[0], m):
                        det_pairs += 1
                        edge_type[frozenset((ids[0], m))] = "deterministic"

        # PASS 1+2 — blocking + exact_fingerprint (the audit's primary rule)
        fp_index: dict[str, list[str]] = defaultdict(list)
        for rid, r in eligible.items():
            fp = self._fingerprint(r)
            if fp is not None:
                fp_index[fp].append(rid)
        fp_pairs = 0
        for fp, ids in fp_index.items():
            if len(ids) < 2:
                continue
            for m in ids[1:]:
                # discriminator veto -> cannot-link
                if self._violates_discriminator(by_id[ids[0]], by_id[m]):
                    cannot_link.add(frozenset((ids[0], m)))
                    continue
                if try_union(ids[0], m):
                    fp_pairs += 1
                    edge_type[frozenset((ids[0], m))] = "exact_fingerprint"

        # PASS 1+2 (fuzzy fallback) — only if profile declares fuzzy rules
        fuzzy_pairs = 0
        if p.fuzzy_rules:
            blocks: dict[str, list[str]] = defaultdict(list)
            for rid, r in eligible.items():
                for keyroles in p.blocking:
                    parts = [p.value(r, role)[:4] if role.endswith("_prefix") else p.value(r, role)
                             for role in keyroles]
                    if all(parts):
                        blocks["|".join(keyroles) + "::" + "|".join(parts)].append(rid)
            seen_pairs: set[frozenset] = set()
            for ids in blocks.values():
                if len(ids) < 2:
                    continue
                for i, a in enumerate(ids):
                    for b in ids[i + 1:]:
                        pk = frozenset((a, b))
                        if pk in seen_pairs:
                            continue
                        seen_pairs.add(pk)
                        if self._fuzzy_score(by_id[a], by_id[b]) >= p.threshold:
                            if self._violates_discriminator(by_id[a], by_id[b]):
                                cannot_link.add(pk)
                                continue
                            if try_union(a, b):
                                fuzzy_pairs += 1
                                edge_type[frozenset((a, b))] = "fuzzy"

        # POST — assemble clusters
        groups: dict[str, list[str]] = defaultdict(list)
        for rid in parent:
            groups[find(rid)].append(rid)

        clusters: list[Cluster] = []
        for root, ids in groups.items():
            if len(ids) < 2:
                continue
            recs = [by_id[i] for i in ids]
            hclass = self._classify_hierarchy(ids, by_id)
            path = self._cluster_path(ids, edge_type)
            conf = 1.0 if path in ("deterministic", "exact_fingerprint") else 0.9
            bucket = self._bucket(recs)
            vstatus, certainty, vreason = self._verify(recs, path, bucket)
            clusters.append(Cluster(
                cluster_id=f"c_{min(ids)}",
                member_ids=sorted(ids),
                fingerprint=self._fingerprint(recs[0]) or "",
                match_path=path,
                confidence=conf,
                hierarchy_class=hclass,
                bucket=bucket,
                certainty=certainty,
                verification_status=vstatus,
                verification_reason=vreason,
                members=[self._snapshot(r) for r in recs],
            ))

        dupe_clusters = [c for c in clusters if c.is_dupe]
        stats = {
            "total_records": len(records),
            "eligible": len(eligible),
            "ineligible": ineligible,
            "clusters_total": len(clusters),
            "clusters_dupe": len(dupe_clusters),
            "accounts_in_dupe_clusters": sum(len(c.member_ids) for c in dupe_clusters),
            "clusters_hierarchy_explained": len(clusters) - len(dupe_clusters),
            "clusters_auto_safe": sum(1 for c in dupe_clusters if c.bucket == "auto_safe"),
            "clusters_needs_review": sum(1 for c in dupe_clusters if c.bucket == "needs_review"),
            "clusters_auto_merge": sum(1 for c in dupe_clusters if c.verification_status == "auto_merge"),
            "clusters_needs_verification": sum(1 for c in dupe_clusters if c.verification_status == "needs_verification"),
            "deterministic_pairs": det_pairs,
            "fingerprint_pairs": fp_pairs,
            "fuzzy_pairs": fuzzy_pairs,
            "vetoed_pairs": len(cannot_link),
        }
        return MatchResult(clusters=clusters, stats=stats)

    # -- hierarchy classification (ParentId induced subgraph) ---------------- #
    def _classify_hierarchy(self, ids: list[str], by_id: dict[str, dict]) -> str:
        if not self.p.parent_role:
            return "disconnected_dupe"
        id_set = set(ids)
        parent_api = self.p._api(self.p.parent_role)
        # build undirected adjacency where an edge exists if one member's ParentId
        # points at another member in the cluster
        adj: dict[str, set] = {i: set() for i in ids}
        same_parent = defaultdict(list)
        for i in ids:
            pid = str(by_id[i].get(parent_api) or "")
            if pid in id_set:
                adj[i].add(pid)
                adj[pid].add(i)
            if pid:
                same_parent[pid].append(i)
        # connected over ALL members via parent/child -> legitimate hierarchy
        start = ids[0]
        seen = {start}
        stack = [start]
        while stack:
            n = stack.pop()
            for m in adj[n]:
                if m not in seen:
                    seen.add(m)
                    stack.append(m)
        if len(seen) == len(id_set):
            return "hierarchy_explained"
        if any(len(v) >= 2 for v in same_parent.values()):
            return "sibling_dupe"
        return "disconnected_dupe"

    # -- verification gate --------------------------------------------------- #
    def _cluster_path(self, ids: list[str], edge_type: dict) -> str:
        """Weakest match type that joined this cluster (fuzzy > fingerprint > deterministic)."""
        id_set = set(ids)
        types = {t for pair, t in edge_type.items() if pair <= id_set}
        for t in ("fuzzy", "exact_fingerprint", "deterministic"):
            if t in types:
                return t
        return "exact_fingerprint"

    def _verify(self, members: list[dict], path: str, bucket: str):
        """Decide whether a cluster may auto-merge or must be human-verified first.

        Config (profile.verification):
          auto_paths        : match paths trusted enough to auto-merge
                              (default ['deterministic','exact_fingerprint'] — fuzzy always verifies)
          require_safe_bucket : if true, only the activity-safe bucket can auto-merge (default true)
          require_discriminators_conclusive : if true, a blank discriminator forces verification
        """
        v = self.p.verification or {}
        auto_paths = v.get("auto_paths", ["deterministic", "exact_fingerprint"])
        require_safe = v.get("require_safe_bucket", True)
        require_disc = v.get("require_discriminators_conclusive", False)

        reasons: list[str] = []
        certain = path in auto_paths
        if not certain:
            reasons.append(f"approximate match ({path}) — verify")

        if certain and require_disc:
            for disc in self.p.discriminators:
                role = disc["role"]
                if any(not self.p.value(m, role) for m in members):
                    certain = False
                    reasons.append(f"discriminator '{role}' blank on some members — couldn't rule out")
                    break

        if certain and require_safe and bucket != "auto_safe":
            reasons.append(f"{bucket.replace('_', ' ')} — verify before merge")
            status = "needs_verification"
        elif certain:
            status = "auto_merge"
        else:
            status = "needs_verification"

        if not reasons:
            reasons.append("deterministic/exact match, activity-safe")
        return status, ("certain" if certain else "review"), "; ".join(reasons)

    # -- bucketing ----------------------------------------------------------- #
    def _bucket(self, recs: list[dict]) -> str:
        p = self.p
        if p.protect_role and any(_truthy(r.get(p._api(p.protect_role))) for r in recs):
            return "needs_review"
        if p.activity_role:
            api = p._api(p.activity_role)
            if all(not (r.get(api)) for r in recs):
                return "auto_safe"
            return "known_active"
        return "needs_review"

    def _snapshot(self, r: dict) -> dict:
        p = self.p
        out = {"Id": p.record_id(r)}
        for role, spec in p.fields.items():
            api = spec.get("api")
            if api:
                out[api] = r.get(api)
        return out


def _truthy(v: Any) -> bool:
    return str(v).strip().lower() in ("true", "1", "yes")
