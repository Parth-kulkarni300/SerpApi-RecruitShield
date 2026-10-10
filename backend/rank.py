"""
rank.py — RecruitShield AI CLI Tool
Run the candidate discovery and ranking pipeline from the command line.

Usage:
    python -m backend.rank --candidates <path/to/candidates.jsonl> --out <output.csv>
    python -m backend.rank --candidates <path/to/candidates.jsonl> --out <output.csv> --jd "<job description text>"
    python -m backend.rank --candidates <path/to/candidates.jsonl> --out <output.csv> --top 50
"""

import argparse
import json
import sys
import pandas as pd
from pathlib import Path

try:
    from backend import ranker
except ImportError:
    import ranker


def main():
    parser = argparse.ArgumentParser(
        description="RecruitShield AI — Candidate Discovery & Ranking CLI Tool"
    )
    parser.add_argument(
        "--candidates",
        type=str,
        required=True,
        help="Path to the candidates JSONL file (e.g. candidates.jsonl)."
    )
    parser.add_argument(
        "--out",
        type=str,
        required=True,
        help="Path to save the ranked output CSV file."
    )
    parser.add_argument(
        "--jd",
        type=str,
        required=False,
        default="",
        help="Job description text to rank candidates against (optional, uses default scoring if omitted)."
    )
    parser.add_argument(
        "--top",
        type=int,
        required=False,
        default=100,
        help="Number of top candidates to include in the output (default: 100)."
    )

    args = parser.parse_args()

    candidates_path = Path(args.candidates)
    output_path = Path(args.out)

    if not candidates_path.exists():
        print(f"Error: Candidates file not found at '{candidates_path}'")
        sys.exit(1)

    # Load candidates
    print(f"Loading candidates from '{candidates_path}'...")
    candidates = []
    with open(candidates_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                candidates.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"Warning: Skipping malformed line — {e}")

    print(f"Loaded {len(candidates)} candidate records.")

    # Run ranking pipeline
    print("Running ranking pipeline...")
    ranked = ranker.rank_candidates(candidates, jd_text=args.jd)

    if not ranked:
        print("No candidates passed the filters. Output will be empty.")
        ranked = []

    shortlist = ranked[: args.top]
    print(f"Shortlisted top {len(shortlist)} candidates.")

    # Normalize scores to [0, 1]
    if shortlist:
        max_score = shortlist[0]["score"] or 1.0
        for c in shortlist:
            c["score"] = round(c["score"] / max_score, 4)

    # Sort by score descending, tie-break by candidate_id ascending
    shortlist.sort(key=lambda x: (-x["score"], x["candidate_id"]))
    for idx, c in enumerate(shortlist):
        c["rank"] = idx + 1

    # Build output rows
    rows = [
        {
            "candidate_id": c["candidate_id"],
            "rank": c["rank"],
            "score": c["score"],
            "reasoning": c.get("reasoning", ""),
        }
        for c in shortlist
    ]

    # Save CSV
    df = pd.DataFrame(rows, columns=["candidate_id", "rank", "score", "reasoning"])
    df.to_csv(output_path, index=False, encoding="utf-8")
    print(f"Saved ranked output to '{output_path}'.")


if __name__ == "__main__":
    main()
