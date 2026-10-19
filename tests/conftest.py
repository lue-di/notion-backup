import pytest

from notion_backup.db import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "backup.db")
    yield s
    s.close()
