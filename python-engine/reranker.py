"""
Multi-Signal Cross-Encoder Reranker for Good Shelves.

Re-scores first-stage retrieval candidates using a weighted combination of:
  - Dense cosine similarity (query vs document embedding)
  - Field-weighted lexical match (title > shelf > tags > body)
  - Phrase-level exact match with position awareness
  - Visual similarity (for images)
  - Recency decay
  - User affinity signals

This is a feature-based learning-to-rank reranker that runs entirely offline
with no additional model downloads. It can be upgraded to a true cross-encoder
ONNX model (e.g. bge-reranker) by swapping the _compute_pair_score method.

The reranker takes the top-N candidates from first-stage retrieval and produces
a re-ordered list with calibrated confidence scores.
"""

from __future__ import annotations

import functools
import math
import re
import time
from typing import Any, Optional

import numpy as np


_RERANK_FIELD_WEIGHTS = {
    "title": 1.0,
    "name": 0.85,
    "shelf": 0.55,
    "tags": 0.50,
    "ocr": 0.35,
    "body": 0.30,
    "meta": 0.20,
}

_RERANK_PHRASE_BOOST = 0.25
_RERANK_TITLE_MATCH_BOOST = 0.20
_RERANK_RECENCY_HALFLIFE_DAYS = 180.0
_RERANK_RECENCY_MAX_BOOST = 0.08
_RERANK_VISUAL_WEIGHT = 0.40
_RERANK_DENSE_WEIGHT = 0.45
_RERANK_LEXICAL_WEIGHT = 0.35


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in re.findall(r"[a-zA-Z0-9]{2,}", text or "")]


@functools.lru_cache(maxsize=16384)
def _stem(token: str) -> str:
    """Lightweight suffix stripper so plurals/gerunds match their roots."""
    if len(token) <= 3:
        return token
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith("sses"):
        return token[:-2]
    if token.endswith(("ches", "shes", "xes", "zes")):
        return token[:-2]
    if token.endswith("ing") and len(token) > 5:
        return token[:-3]
    if token.endswith("ed") and len(token) > 4:
        return token[:-2]
    if token.endswith("es") and len(token) > 4:
        return token[:-2]
    if token.endswith("s") and not token.endswith("ss") and len(token) > 3:
        return token[:-1]
    return token


def _token_and_stem_set(text: str) -> set[str]:
    toks = _tokenize(text)
    return set(toks) | {_stem(t) for t in toks}


def _term_weight(term: str, idf_map: Optional[dict[str, float]] = None) -> float:
    if idf_map:
        st = _stem(term)
        if term in idf_map or st in idf_map:
            return max(idf_map.get(term, 0.0), idf_map.get(st, 0.0), 0.1)
    return max(1.0, min(2.2, 0.7 + 0.15 * len(term)))


def _term_matches_tokens(term: str, tokens_with_stems: set[str]) -> bool:
    return term in tokens_with_stems or _stem(term) in tokens_with_stems


def _field_score_from_tokens(
    tokens: set[str],
    query_terms: set[str],
    weight: float,
    idf_map: Optional[dict[str, float]] = None,
) -> float:
    if not tokens or not query_terms:
        return 0.0
    matched_w = 0.0
    total_w = 0.0
    overlap = 0
    for term in query_terms:
        tw = _term_weight(term, idf_map)
        total_w += tw
        if _term_matches_tokens(term, tokens):
            matched_w += tw
            overlap += 1
    if overlap == 0:
        return 0.0
    coverage = matched_w / max(1e-6, total_w)
    saturation = 1.0 - math.exp(-overlap * 1.5)
    return weight * coverage * saturation


def _field_score(
    field_text: str,
    query_terms: set[str],
    weight: float,
    idf_map: Optional[dict[str, float]] = None,
) -> float:
    if not field_text or not query_terms:
        return 0.0
    return _field_score_from_tokens(_token_and_stem_set(field_text), query_terms, weight, idf_map)


def _phrase_boost(text: str, phrase: str) -> float:
    if not text or not phrase:
        return 0.0
    text_low = text.lower()
    phrase_low = phrase.lower()
    if phrase_low in text_low:
        return _RERANK_PHRASE_BOOST
    words = phrase_low.split()
    if len(words) >= 3:
        for i in range(len(words) - 1):
            bigram = f"{words[i]} {words[i + 1]}"
            if len(bigram) >= 5 and bigram in text_low:
                return 0.65 * _RERANK_PHRASE_BOOST
    return 0.0


def _recency_boost(mtime: float, now: float, has_recency_intent: bool = False) -> float:
    if mtime <= 0:
        return 0.0
    age_days = max(0.0, (now - mtime) / 86400.0)
    max_boost = _RERANK_RECENCY_MAX_BOOST if has_recency_intent else 0.015
    return max_boost * math.exp(-age_days / _RERANK_RECENCY_HALFLIFE_DAYS)


_INTENT_PROFILES: dict[str, dict[str, float]] = {
    "general": {"dense": 1.0, "lexical": 1.0, "phrase": 1.0, "title": 1.0, "visual": 1.0, "entity_meta": 1.0, "fts": 0.12},
    "title": {"dense": 0.85, "lexical": 1.18, "phrase": 1.25, "title": 1.65, "visual": 0.85, "entity_meta": 1.0, "fts": 0.14},
    "visual": {"dense": 0.90, "lexical": 0.85, "phrase": 0.90, "title": 0.95, "visual": 1.50, "entity_meta": 0.85, "fts": 0.08},
    "entity": {"dense": 0.85, "lexical": 1.25, "phrase": 1.15, "title": 1.05, "visual": 0.75, "entity_meta": 1.65, "fts": 0.15},
    "lexical": {"dense": 0.80, "lexical": 1.35, "phrase": 1.45, "title": 1.25, "visual": 0.80, "entity_meta": 1.15, "fts": 0.18},
}


def detect_rerank_intent(query: str) -> str:
    """Infer optimal feature weight profile from query shape."""
    q = (query or "").strip()
    if not q:
        return "general"
    q_low = q.lower()
    if re.search(r"\.[a-z0-9]{2,5}\b", q_low):
        return "title"
    if '"' in q or "'" in q or re.search(r"[A-Z]{2,}-\d+|_[a-z0-9]+", q):
        return "lexical"
    if re.search(r"\b(receipt|invoice|bill|merchant|vendor|amount|total|paid|spent|due|tax|statement|php|usd)\b", q_low) or "$" in q or "₱" in q:
        return "entity"
    if re.search(r"\b(photo|picture|image|pic|selfie|sunset|beach|cat|dog|car|flower|mountain|portrait|screenshot|drawing|logo|diagram)\b", q_low):
        return "visual"
    toks = _tokenize(q)
    if 1 <= len(toks) <= 3:
        return "title"
    return "general"


def _compute_pair_score(
    query: str,
    query_terms: set[str],
    query_embedding: Optional[np.ndarray],
    candidate: dict[str, Any],
    now: float,
    idf_map: Optional[dict[str, float]] = None,
    has_recency_intent: bool = False,
    query_intent: str = "general",
) -> tuple[float, dict[str, float]]:
    """
    Compute a rerank score for a single query-document pair using intent-aware feature weights
    and optional FTS5 BM25 fusion.
    Returns (score, feature_breakdown).
    """
    prof = _INTENT_PROFILES.get(query_intent, _INTENT_PROFILES["general"])
    features: dict[str, float] = {}

    # Prefer a precomputed cosine similarity when the caller already has one
    # (avoids re-embedding and keeps semantic matches intact).
    dense_sim = candidate.get("_dense_sim")
    if dense_sim is None:
        doc_embedding = candidate.get("_embedding")
        dense_sim = 0.0
        if doc_embedding is not None and query_embedding is not None:
            try:
                qn = float(np.linalg.norm(query_embedding))
                dn = float(np.linalg.norm(doc_embedding))
                if qn > 1e-9 and dn > 1e-9:
                    dense_sim = float(np.clip(np.dot(query_embedding, doc_embedding) / (qn * dn), 0.0, 1.0))
            except Exception:
                dense_sim = 0.0
    dense_sim = float(max(0.0, min(1.0, dense_sim)))
    features["dense"] = dense_sim

    title_text = f"{candidate.get('title', '')} {candidate.get('name', '')}"
    shelf_text = str(candidate.get("shelf_name", candidate.get("shelfName", "")))
    tags_text = " ".join(candidate.get("tags", [])) if isinstance(candidate.get("tags"), list) else str(candidate.get("tags_json", ""))
    ocr_text = str(candidate.get("ocr_text", candidate.get("ocrText", "")))
    body_text = str(candidate.get("extracted_text", candidate.get("extractedText", "")))
    meta_text = str(candidate.get("metadata_json", candidate.get("metadata", "")))

    title_tokens = _token_and_stem_set(title_text)
    shelf_tokens = _token_and_stem_set(shelf_text)
    tags_tokens = _token_and_stem_set(tags_text)
    ocr_tokens = _token_and_stem_set(ocr_text)
    body_tokens = _token_and_stem_set(body_text)
    meta_tokens = _token_and_stem_set(meta_text)

    meta_mult = prof["entity_meta"]
    lex_score = (
        _field_score_from_tokens(title_tokens, query_terms, _RERANK_FIELD_WEIGHTS["title"], idf_map=idf_map)
        + _field_score_from_tokens(shelf_tokens, query_terms, _RERANK_FIELD_WEIGHTS["shelf"], idf_map=idf_map)
        + _field_score_from_tokens(tags_tokens, query_terms, _RERANK_FIELD_WEIGHTS["tags"], idf_map=idf_map)
        + _field_score_from_tokens(ocr_tokens, query_terms, _RERANK_FIELD_WEIGHTS["ocr"] * meta_mult, idf_map=idf_map)
        + _field_score_from_tokens(body_tokens, query_terms, _RERANK_FIELD_WEIGHTS["body"], idf_map=idf_map)
        + _field_score_from_tokens(meta_tokens, query_terms, _RERANK_FIELD_WEIGHTS["meta"] * meta_mult, idf_map=idf_map)
    )
    if len(query_terms) >= 2:
        all_doc_tokens = title_tokens | shelf_tokens | tags_tokens | ocr_tokens | body_tokens | meta_tokens
        matched_terms_across_doc = sum(
            1 for t in query_terms if _term_matches_tokens(t, all_doc_tokens)
        )
        doc_cov = matched_terms_across_doc / max(1, len(query_terms))
        lex_score = lex_score * (0.50 + 0.50 * doc_cov)
    lex_score = min(1.0, lex_score)
    features["lexical"] = lex_score

    phrase = " ".join(_tokenize(query))
    phrase_s = _phrase_boost(title_text, phrase) + _phrase_boost(body_text + " " + ocr_text, phrase)
    phrase_s = min(0.3, phrase_s) * prof["phrase"]
    features["phrase"] = phrase_s

    title_s = 0.0
    if query_terms:
        title_low = title_text.lower()
        t_matched_w = 0.0
        t_total_w = 0.0
        for t in query_terms:
            tw = _term_weight(t, idf_map)
            t_total_w += tw
            if _term_matches_tokens(t, title_tokens) or t in title_low:
                t_matched_w += tw
        title_term_coverage = t_matched_w / max(1e-6, t_total_w)
        title_s = _RERANK_TITLE_MATCH_BOOST * title_term_coverage * prof["title"]
    features["title_match"] = title_s

    recency_s = _recency_boost(
        float(candidate.get("mtime", candidate.get("_mtime", now))),
        now,
        has_recency_intent=has_recency_intent,
    )
    features["recency"] = recency_s

    visual_s = 0.0
    modality = str(candidate.get("modality", candidate.get("_modality", "")))
    if modality == "image":
        vis_sim = float(candidate.get("_vis_sim", 0.0))
        visual_s = max(0.0, vis_sim) * _RERANK_VISUAL_WEIGHT * prof["visual"]
    features["visual"] = visual_s

    affinity_s = min(0.15, max(0.0, float(candidate.get("_affinity_boost", 0.0))))
    features["affinity"] = affinity_s

    # Optional FTS5 BM25 normalized score fusion (0..1)
    fts_raw = float(candidate.get("_fts_score", 0.0) or 0.0)
    fts_s = max(0.0, min(1.0, fts_raw)) * prof["fts"]
    features["fts_bm25"] = fts_s

    final = (
        _RERANK_DENSE_WEIGHT * prof["dense"] * dense_sim
        + _RERANK_LEXICAL_WEIGHT * prof["lexical"] * lex_score
        + phrase_s
        + title_s
        + recency_s
        + visual_s
        + affinity_s
        + fts_s
    )
    # Squash the additive score into [0, 1) so downstream score blending is calibrated.
    final = final / (1.0 + final) if final > 0 else 0.0
    return final, features


def rerank_candidates(
    query: str,
    candidates: list[dict[str, Any]],
    query_embedding: Optional[np.ndarray] = None,
    top_n: int = 50,
    now: Optional[float] = None,
    idf_map: Optional[dict[str, float]] = None,
    has_recency_intent: bool = False,
    query_intent: str = "auto",
) -> list[dict[str, Any]]:
    """
    Re-score and re-order first-stage retrieval candidates with intent-aware weights
    and optional FTS5 BM25 signal fusion.
    """
    if not candidates:
        return []

    if now is None:
        now = time.time()

    resolved_intent = detect_rerank_intent(query) if query_intent == "auto" else query_intent
    query_terms = set(_tokenize(query))

    scored: list[tuple[float, dict[str, Any]]] = []
    for cand in candidates:
        score, features = _compute_pair_score(
            query,
            query_terms,
            query_embedding,
            cand,
            now,
            idf_map=idf_map,
            has_recency_intent=has_recency_intent,
            query_intent=resolved_intent,
        )
        enriched = dict(cand)
        enriched["_rerank_score"] = round(score, 6)
        enriched["_rerank_features"] = features
        scored.append((score, enriched))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in scored[:top_n]]