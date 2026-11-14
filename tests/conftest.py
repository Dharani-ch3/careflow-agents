from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from careflow.config import Settings
from careflow.seed import seed

# A Monday, so "next week", "Friday" etc. resolve predictably.
TODAY = date(2026, 11, 16)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(data_dir=tmp_path, today=TODAY, llm_provider="offline")
    seed(s.ehr_db, today=TODAY).close()
    return s
