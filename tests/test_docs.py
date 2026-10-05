"""Документация, которую можно проверить автоматически, совпадает с кодом."""

import re
from pathlib import Path

from app.config import Settings
from scripts.dump_mcp_contracts import OUT, render

ROOT = Path(__file__).resolve().parent.parent


async def test_mcp_contracts_doc_matches_running_server():
    # Если тест упал — выполните: python -m scripts.dump_mcp_contracts
    assert OUT.read_text(encoding="utf-8") == await render()


def test_every_setting_is_documented_in_configuration_and_env_example():
    documented = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    names = [name.upper() for name in Settings.__dataclass_fields__]
    missing_docs = [name for name in names if f"`{name}`" not in documented]
    missing_example = [name for name in names if not re.search(rf"^{name}=", example, re.M)]
    assert missing_docs == []
    assert missing_example == []
