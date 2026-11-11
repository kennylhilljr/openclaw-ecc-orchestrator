import sys
import tempfile
from pathlib import Path

REPO_SRC = Path(__file__).resolve().parent.parent / "src"
if str(REPO_SRC) not in sys.path:
    sys.path.insert(0, str(REPO_SRC))

_default_root = Path(tempfile.gettempdir()).resolve()
if any(part in (".openclaw", ".git") for part in _default_root.parts):
    fresh = tempfile.mkdtemp(dir="/tmp", prefix="ecc-tests-")
    tempfile.tempdir = fresh
