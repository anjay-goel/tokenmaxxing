from pathlib import Path

import pytest


MINIMAL_CONFIG = """\
version: 1
profile:
  name: Ada Lovelace
  bio: Programmer
  avatar: avatar.webp
  links: []
site:
  title: Ada's token trail
  description: Aggregate local AI agent usage.
  canonical_url: https://example.com/tokens/
  indexable: true
  timezone: UTC
  theme: auto
  accent: violet
metrics:
  window_days: 28
deploy:
  command: [fake-deploy, "{site_dir}"]
schedule:
  time: "09:00"
"""


@pytest.fixture
def minimal_config() -> str:
    return MINIMAL_CONFIG


@pytest.fixture(autouse=True)
def isolated_tokenmaxxing_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    path = tmp_path / "tokenmaxxing-home"
    monkeypatch.setenv("TOKENMAXXING_HOME", str(path))
    return path


@pytest.fixture
def profile_config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(MINIMAL_CONFIG, encoding="utf-8")
    (tmp_path / "avatar.webp").write_bytes(b"avatar")
    return path
