import shutil
from pathlib import Path

for folder in [Path("qdrant_data"), Path("rag_cache")]:
    if folder.exists():
        shutil.rmtree(folder)
        print(f"Deleted {folder}")
    else:
        print(f"Not found: {folder}")