"""Import first in every entry script: when started with a Python that lacks the project's packages
(e.g. plain `python3 src/fetch_positions.py`), re-run the same command with .venv/bin/python."""

import os
import sys
from pathlib import Path

try:
    import numpy  # noqa: F401
    import scipy  # noqa: F401
    import sgp4  # noqa: F401
except ModuleNotFoundError as err:
    venv_python = Path(__file__).resolve().parent.parent / ".venv" / "bin" / "python"
    if venv_python.exists() and not os.environ.get("FUSION_VENV_REEXEC"):
        os.environ["FUSION_VENV_REEXEC"] = "1"
        os.execv(str(venv_python), [str(venv_python), *sys.argv])
    sys.exit(f"Missing package '{err.name}'. Run ./run.sh once (it creates .venv), or:\n"
             "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt")
