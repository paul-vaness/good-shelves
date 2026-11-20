#!/usr/bin/env python3
"""
Automated Retrieval & Clustering Benchmark Suite for Good Shelves (PROL301 Research Evaluation).
Evaluates Precision@5, Recall@10, Mean Reciprocal Rank (MRR), Silhouette Score,
Query Latency (ms), and Process RAM Footprint (MB) against the real files in your watched folders.
"""

from __future__ import annotations

import argparse
import json
import re
import tempfile
import time
from pathlib import Path
from typing import Any

import numpy as np
import psutil

from vector_store import VectorStore


_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "into", "your", "you",
    "are", "was", "were", "have", "has", "had", "not", "but", "what", "all",
    "can", "out", "use", "used", "using", "file", "files", "page", "pages",
    "final", "draft", "revised", "copy", "new", "old", "v1", "v2", "v3",
}


def _is_relevant(file_item: dict[str, Any], keywords: list[str]) -> bool:
    hay = (
        f"{file_item['name']} {file_item['originalName']} {file_item['title']} "
        f"{file_item['extractedText']}"
    ).lower()
    return any(bool(re.search(rf"\b{re.escape(k.lower())}\b", hay)) for k in keywords)


def _keywords_for_file(file_item: dict[str, Any]) -> list[str]:
    text = f"{file_item['title']} {Path(file_item['name']).stem.replace('_', ' ').replace('-', ' ')}"
    words: list[str] = []
    for word in re.findall(r"[a-zA-Z][a-zA-Z0-9-]{2,}", text):
        lowered = word.lower()
        if lowered in _STOPWORDS or lowered in words:
            continue
        words.append(lowered)
        if len(words) >= 5:
            break
    return words


def _build_eval_queries(all_files: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    """Derive self-retrieval queries from real file titles, one per shelf where possible."""
    queries: list[dict[str, Any]] = []
    seen_shelves: set[str] = set()
    for f in all_files:
        shelf = str(f.get("shelfId", ""))
        if shelf in seen_shelves or shelf == "tray":
            continue
        keywords = _keywords_for_file(f)
        if len(keywords) < 2:
            continue
        seen_shelves.add(shelf)
        queries.append(
            {
                "query": " ".join(keywords[:3]),
                "relevant_keywords": keywords,
                "source_file": f["name"],
            }
        )
        if len(queries) >= limit:
            break
    return queries


def run_benchmark(target_folder: Path | None = None, output_json: Path | None = None) -> dict[str, Any]:
    process = psutil.Process()
    ram_before_mb = process.memory_info().rss / (1024.0 * 1024.0)

    t_init_start = time.perf_counter()
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "benchmark_index.db"
        store = VectorStore(db_path=db_path)

        if target_folder and target_folder.exists():
            store.scan_folder(target_folder, max_files=250, recursive=True)
        else:
            for folder in store.get_watched_folders():
                if folder["active"]:
                    store.scan_folder(Path(folder["path"]), max_files=150, recursive=True)

        cluster_info = store.recluster_all()
        state = store.get_all_state()
        all_files = state["files"]
        t_index_total_ms = (time.perf_counter() - t_init_start) * 1000.0

        eval_queries = _build_eval_queries(all_files)

        precisions_at_5: list[float] = []
        recalls_at_10: list[float] = []
        reciprocal_ranks: list[float] = []
        latencies_ms: list[float] = []
        per_query_rows: list[dict[str, Any]] = []

        for eq in eval_queries:
            q_str = eq["query"]
            rel_kw = eq["relevant_keywords"]
            total_rel = max(1, sum(1 for f in all_files if _is_relevant(f, rel_kw)))

            t0 = time.perf_counter()
            search_res = store.search_semantic(query=q_str, modality_filter="all", limit=10, min_similarity=0.10)
            lat_ms = (time.perf_counter() - t0) * 1000.0
            latencies_ms.append(lat_ms)

            ranked = search_res["results"]
            top5 = ranked[:5]
            top10 = ranked[:10]

            hits_top5 = sum(1 for f in top5 if _is_relevant(f, rel_kw))
            hits_top10 = sum(1 for f in top10 if _is_relevant(f, rel_kw))

            denom_p5 = float(min(5, total_rel))
            p_at_5 = hits_top5 / denom_p5
            r_at_10 = min(1.0, hits_top10 / float(total_rel))

            rr = 0.0
            for rank_idx, f in enumerate(top10, start=1):
                if _is_relevant(f, rel_kw):
                    rr = 1.0 / float(rank_idx)
                    break

            precisions_at_5.append(p_at_5)
            recalls_at_10.append(r_at_10)
            reciprocal_ranks.append(rr)

            per_query_rows.append(
                {
                    "query": q_str,
                    "source_file": eq["source_file"],
                    "top1_match": ranked[0]["name"] if ranked else "none",
                    "top1_cosine": ranked[0]["cosine"] if ranked else "0.000",
                    "P@5": round(p_at_5, 4),
                    "Recall@10": round(r_at_10, 4),
                    "RR": round(rr, 4),
                    "latency_ms": round(lat_ms, 2),
                }
            )

        ram_peak_mb = process.memory_info().rss / (1024.0 * 1024.0)

        def _mean(values: list[float]) -> float:
            return float(np.mean(values)) if values else 0.0

        report = {
            "system": "Good Shelves Multimodal Semantic File Manager v1.0",
            "authors": [
                "Paul Vaness B. Ramos",
                "Ishee Margareth B. Esteleydes",
                "Ayessa S. Digno",
            ],
            "institution": "Calaca City Global College · Computer Science Department (PROL301)",
            "corpus_size": len(all_files),
            "discovered_shelves": len(state["shelves"]),
            "lookalike_pairs_found": len(state["lookalikes"]),
            "metrics": {
                "Precision@5": round(_mean(precisions_at_5), 4),
                "Recall@10": round(_mean(recalls_at_10), 4),
                "MRR": round(_mean(reciprocal_ranks), 4),
                "Silhouette_Score": round(float(cluster_info["silhouette"]), 4),
                "Mean_Query_Latency_ms": round(_mean(latencies_ms), 2),
                "P95_Query_Latency_ms": round(_mean([float(np.percentile(latencies_ms, 95))]), 2) if latencies_ms else 0.0,
                "Total_Indexing_Time_ms": round(t_index_total_ms, 1),
                "RAM_Footprint_MB": round(ram_peak_mb, 2),
                "RAM_Delta_MB": round(max(1.0, ram_peak_mb - ram_before_mb), 2),
            },
            "queries": per_query_rows,
        }

    # Print formatted research report
    m = report["metrics"]
    print("=" * 78)
    print(" GOOD SHELVES — AUTOMATED MULTIMODAL RETRIEVAL & CLUSTERING BENCHMARK REPORT")
    print(" Calaca City Global College · Computer Science Department · PROL301")
    print("=" * 78)
    print(f" Indexed Files        : {report['corpus_size']} real local files")
    print(f" Discovered Shelves   : {report['discovered_shelves']} (UMAP 512-d -> 5-d + HDBSCAN)")
    print(f" Near-Duplicate Pairs : {report['lookalike_pairs_found']} (Cosine >= 0.92 / Perceptual dHash)")
    print("-" * 78)
    print(f" Precision@5 (P@5)    : {m['Precision@5']:.4f}  ({m['Precision@5']*100:.1f}%)")
    print(f" Recall@10 (R@10)     : {m['Recall@10']:.4f}  ({m['Recall@10']*100:.1f}%)")
    print(f" Mean Reciprocal Rank : {m['MRR']:.4f}")
    print(f" Silhouette Score     : {m['Silhouette_Score']:.4f}")
    print(f" Mean Query Latency   : {m['Mean_Query_Latency_ms']:.2f} ms (P95: {m['P95_Query_Latency_ms']:.2f} ms)")
    print(f" Process RAM Footprint: {m['RAM_Footprint_MB']:.2f} MB")
    print("-" * 78)
    print(f" {'Query':<40} | {'Top-1 File':<24} | {'P@5':<5} | {'R@10':<5} | {'Lat(ms)':<7}")
    print("-" * 78)
    for q in report["queries"]:
        print(
            f" {q['query'][:40]:<40} | {q['top1_match'][:24]:<24} | "
            f"{q['P@5']:<5.2f} | {q['Recall@10']:<5.2f} | {q['latency_ms']:<7.2f}"
        )
    print("=" * 78)

    if output_json:
        output_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f" Saved JSON benchmark report to {output_json}")

    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run Good Shelves retrieval & clustering benchmark")
    parser.add_argument("--folder", type=str, default=None, help="Optional extra local folder to scan")
    parser.add_argument(
        "--out",
        type=str,
        default=str(Path(__file__).resolve().parent / "benchmark_report.json"),
        help="Path to write JSON benchmark report",
    )
    args = parser.parse_args()
    folder_p = Path(args.folder).resolve() if args.folder else None
    run_benchmark(target_folder=folder_p, output_json=Path(args.out))
