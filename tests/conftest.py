"""Shared pytest fixtures for claude-code-monitor tests."""
from __future__ import annotations

import sys
from pathlib import Path

# Make the project root importable so tests can `import cc_history`.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
