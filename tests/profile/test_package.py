import json
import os
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    output = tmp_path_factory.mktemp("wheel")
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(output)],
        cwd=ROOT,
        check=True,
        shell=False,
        capture_output=True,
        text=True,
    )
    return next(output.glob("*.whl"))


def _environment_python(environment: Path) -> Path:
    return environment / ("Scripts" if sys.platform == "win32" else "bin") / (
        "python.exe" if sys.platform == "win32" else "python"
    )


def _tokenmaxxing_executable(environment: Path) -> Path:
    return environment / ("Scripts" if sys.platform == "win32" else "bin") / (
        "tokenmaxxing.exe" if sys.platform == "win32" else "tokenmaxxing"
    )


def _clean_environment(state: Path) -> dict[str, str]:
    environ = os.environ.copy()
    for name in ("PYTHONPATH", "UV_PROJECT_ENVIRONMENT", "VIRTUAL_ENV"):
        environ.pop(name, None)
    environ["TOKENMAXXING_HOME"] = str(state)
    return environ


def test_built_wheel_contains_every_profile_resource(built_wheel: Path) -> None:
    assert built_wheel.name.startswith("tokenmaxxing-0.1.0-")
    with ZipFile(built_wheel) as archive:
        names = set(archive.namelist())
        metadata = archive.read("tokenmaxxing-0.1.0.dist-info/METADATA").decode()

    assert "Name: tokenmaxxing\n" in metadata

    required = {
        "tokenmaxxing/data/rate-card.json",
        "tokenmaxxing/profile/assets/ASSET_SOURCES.md",
        "tokenmaxxing/profile/assets/profile.css",
        "tokenmaxxing/profile/assets/profile.js",
        "tokenmaxxing/profile/assets/licenses/Lobe-Icons-MIT.txt",
        "tokenmaxxing/profile/assets/fonts/OFL.txt",
        "tokenmaxxing/profile/assets/fonts/NOTICE.md",
        "tokenmaxxing/profile/starters/custom.css",
        "tokenmaxxing/profile/starters/gitignore",
        "tokenmaxxing/profile/starters/config.yaml",
        "tokenmaxxing/profile/templates/index.html.j2",
        "tokenmaxxing/profile/templates/partials/activity.html.j2",
        "tokenmaxxing/profile/templates/partials/agents.html.j2",
        "tokenmaxxing/profile/templates/partials/header.html.j2",
        "tokenmaxxing/profile/templates/partials/models.html.j2",
        "tokenmaxxing/profile/templates/partials/trend.html.j2",
    }
    assert required <= names
    model_icons = {
        "claude",
        "deepseek",
        "google",
        "mistral",
        "moonshot",
        "openai",
        "opencode",
        "pi",
        "qwen",
        "xai",
        "zai",
        "ai2",
        "ai21",
        "ai360",
        "baichuan",
        "baidu",
        "bedrock",
        "chatglm",
        "cohere",
        "dbrx",
        "doubao",
        "gemma",
        "huggingface",
        "hunyuan",
        "ibm",
        "internlm",
        "kimi",
        "liquid",
        "longcat",
        "meta",
        "microsoft",
        "minimax",
        "nvidia",
        "perplexity",
        "rwkv",
        "sensenova",
        "skywork",
        "snowflake",
        "spark",
        "stepfun",
        "tii",
        "wenxin",
        "xiaomimimo",
        "yi",
    }
    assert {
        f"tokenmaxxing/profile/assets/icons/{name}.svg" for name in model_icons
    } <= names
    assert len([name for name in names if "/assets/fonts/" in name and name.endswith(".woff2")]) == 4

    forbidden = ("tokenmaxxing-export.json", "tokenmaxxing.sqlite3", "-wal", "-shm", "/salt")
    assert not any(marker in name for name in names for marker in forbidden)
    assert not any("/.tokenmaxxing/" in name for name in names)


def test_installed_wheel_initializes_and_builds_outside_checkout(
    built_wheel: Path, tmp_path: Path
) -> None:
    environment = tmp_path / "environment"
    work = tmp_path / "outside-checkout"
    work.mkdir()
    subprocess.run(
        ["uv", "venv", "--python", sys.executable, str(environment)],
        check=True,
        shell=False,
        capture_output=True,
        text=True,
    )
    python = _environment_python(environment)
    subprocess.run(
        ["uv", "pip", "install", "--python", str(python), str(built_wheel)],
        check=True,
        shell=False,
        capture_output=True,
        text=True,
    )
    executable = _tokenmaxxing_executable(environment)
    environ = _clean_environment(work / "state")

    help_result = subprocess.run(
        [str(executable), "--help"],
        cwd=work,
        env=environ,
        check=True,
        shell=False,
        capture_output=True,
        text=True,
    )
    assert "profile" in help_result.stdout

    project = work / "profile"
    subprocess.run(
        [str(executable), "profile", "init", str(project), "--no-setup"],
        cwd=work,
        env=environ,
        check=True,
        shell=False,
        capture_output=True,
        text=True,
    )
    database = work / "usage.sqlite3"
    subprocess.run(
        [str(executable), "--db", str(database), "stats", "--json"],
        cwd=work,
        env=environ,
        check=True,
        shell=False,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(
        [
            str(executable),
            "--db",
            str(database),
            "profile",
            "build",
            "--json",
        ],
        cwd=work,
        env=environ,
        check=True,
        shell=False,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    site = Path(payload["site_dir"])
    assert site == (project / "dist").resolve()
    assert (site / "index.html").is_file()
    assert (site / "profile.json").is_file()
    assert (site / "assets" / "profile.css").is_file()
    assert not Path(payload["site_dir"]).is_relative_to(ROOT)
