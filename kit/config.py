"""Settings and secrets.

Secrets load from ~/.config/process-review-agent/.env first, so no key file sits inside the repo
(or on a shared screen). A repo-local .env is honoured second and is gitignored.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
USER_ENV = Path.home() / ".config" / "process-review-agent" / ".env"

load_dotenv(USER_ENV, override=False)
load_dotenv(REPO_ROOT / ".env", override=False)

# "quality" (default) or "fast" — fast swaps every role to lower-latency models.
PROFILE = os.getenv("KIT_PROFILE", "quality")
RUNS_DIR = Path(os.getenv("KIT_RUNS_DIR", REPO_ROOT / "runs"))
