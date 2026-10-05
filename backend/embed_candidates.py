import argparse
import json
import numpy as np
import os
from pathlib import Path
import time

try:
    from sentence_transformers import SentenceTransformer
except ImportError:
    print("Error: sentence-transformers is not installed. Please run: pip install sentence-transformers")
    exit(1)

def main():
    parser = argparse.ArgumentParser(description="Pre-compute candidate embeddings for semantic ranking.")
    parser.add_argument("--candidates", type=str, default=None, help="Absolute path to candidates.jsonl")
    args = parser.parse_args()

    # Setup paths
    backend_dir = Path(__file__).parent
    if args.candidates:
        candidates_file = Path(args.candidates)
    else:
        # Prefer a real candidates.jsonl if one's been placed alongside this script,
        # otherwise fall back to the small sample dataset bundled with the repo.
        candidates_file = backend_dir / "candidates.jsonl"
        if not candidates_file.exists():
            candidates_file = backend_dir / "sample_candidates.jsonl"
    output_embeddings_file = backend_dir / "candidate_embeddings.npy"
    
    if not candidates_file.exists():
        print(f"Error: Candidate database not found at {candidates_file}")
        return
        
    print("Initializing SentenceTransformer model (BAAI/bge-base-en-v1.5)...")
    # Using state-of-the-art 768-dim retrieval model
    model = SentenceTransformer('BAAI/bge-base-en-v1.5')
    
    print("Loading candidate profiles from database...")
    candidates = []
    texts = []
    
    start_time = time.time()
    with open(candidates_file, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            cand = json.loads(line)
            candidates.append(cand["candidate_id"])
            
            # Combine current title, headline, and summary into a semantic representation
            profile = cand.get("profile", {})
            title = profile.get("current_title", "")
            headline = profile.get("headline", "")
            summary = profile.get("summary", "")
            
            combined_text = f"Title: {title}. Headline: {headline}. Summary: {summary}"
            texts.append(combined_text)
            
    total_candidates = len(candidates)
    print(f"Loaded {total_candidates} candidates in {time.time() - start_time:.2f} seconds.")
    
    # Process in batches to prevent memory overflow and show progress
    batch_size = 512
    all_embeddings = []
    
    print(f"Generating embeddings using CPU/GPU in batches of {batch_size}...")
    start_encode_time = time.time()
    
    for i in range(0, total_candidates, batch_size):
        batch_texts = texts[i : i + batch_size]
        # encode the batch (normalize_embeddings=True gives unit length vectors for easy cosine similarity)
        batch_embeddings = model.encode(batch_texts, show_progress_bar=False, normalize_embeddings=True)
        all_embeddings.append(batch_embeddings)
        
        # Periodic logging
        if (i + batch_size) % 10240 == 0 or (i + batch_size) >= total_candidates:
            elapsed = time.time() - start_encode_time
            processed = min(i + batch_size, total_candidates)
            speed = processed / elapsed
            est_remaining = (total_candidates - processed) / speed if speed > 0 else 0
            print(f"  Processed {processed}/{total_candidates} candidates ({processed/total_candidates*100:.1f}%) | "
                  f"Speed: {speed:.1f} cands/sec | Est. Remaining: {est_remaining/60:.1f} mins")
            
    # Concatenate and save embeddings
    embeddings_matrix = np.vstack(all_embeddings)
    print(f"Finished generating embeddings in {(time.time() - start_encode_time)/60:.2f} minutes.")
    
    print(f"Saving embeddings matrix to {output_embeddings_file}...")
    np.save(output_embeddings_file, embeddings_matrix)
    
    # Also save the candidate IDs list to make sure index maps correctly
    output_ids_file = backend_dir / "candidate_ids.json"
    print(f"Saving candidate ID mappings to {output_ids_file}...")
    with open(output_ids_file, "w", encoding="utf-8") as f:
        json.dump(candidates, f)
        
    print("Offline embedding pre-computation complete! The ranking engine can now load these files for instant semantic similarity.")

if __name__ == '__main__':
    main()
