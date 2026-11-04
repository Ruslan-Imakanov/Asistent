import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for k in ("ANTHROPIC_API_KEY", "SMTP_HOST", "IMAP_HOST", "TELEGRAM_BOT_TOKEN", "STRIPE_API_KEY", "OFFICE_ADMIN_TOKEN"):
    os.environ.pop(k, None)

from office.config import load_config  # noqa: E402
from office.llm import DemoLLM  # noqa: E402
from office.office import Office  # noqa: E402


@pytest.fixture
def office(tmp_path, monkeypatch):
    monkeypatch.setattr("office.config.load_dotenv", lambda p: None)
    cfg = load_config(data_dir=tmp_path / "data", workspace_dir=tmp_path / "ws")
    cfg.autonomy["tick_seconds"] = 0
    return Office(cfg=cfg, llm=DemoLLM())
