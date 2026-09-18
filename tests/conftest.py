import os
import sys
from pathlib import Path

# Tests never spend API credits: the deterministic canned interpreter is used.
os.environ.setdefault("MOCK_LLM", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
