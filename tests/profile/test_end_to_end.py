import json
import sys
from pathlib import Path

import yaml

from tokenmaxxing.cli import main
from tokenmaxxing.db import Database
from tokenmaxxing.profile.project import profile_paths


def _initialize_database(path: Path) -> None:
    database = Database.open(path)
    database.close()


def _configure_deploy(config_path: Path, script: Path, marker: Path) -> None:
    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    document["deploy"]["command"] = [
        sys.executable,
        str(script),
        "{site_dir}",
        str(marker),
    ]
    config_path.write_text(
        yaml.safe_dump(document, sort_keys=False),
        encoding="utf-8",
    )


def test_init_build_and_fake_publish_work_together(
    tmp_path: Path, capsys
) -> None:
    project = tmp_path / "profile"
    database = tmp_path / "usage.sqlite3"
    marker = tmp_path / "published.json"
    deploy = tmp_path / "deploy.py"
    deploy.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "site, marker = map(Path, sys.argv[1:])\n"
        "marker.write_text((site / 'profile.json').read_text(encoding='utf-8'), encoding='utf-8')\n",
        encoding="utf-8",
    )

    assert main(["profile", "init", str(project), "--no-setup"]) == 0
    capsys.readouterr()
    _initialize_database(database)
    config_path = project / "config.yaml"
    _configure_deploy(config_path, deploy, marker)

    assert (
        main(
            [
                "--db",
                str(database),
                "profile",
                "--config",
                str(config_path),
                "build",
                "--json",
            ]
        )
        == 0
    )
    build_payload = json.loads(capsys.readouterr().out)
    paths = profile_paths(config_path)
    assert (
        main(
            [
                "--db",
                str(database),
                "profile",
                "--config",
                str(config_path),
                "publish",
                "--non-interactive",
                "--json",
            ]
        )
        == 0
    )
    publish_payload = json.loads(capsys.readouterr().out)

    assert Path(build_payload["site_dir"]) == paths.site
    assert publish_payload["deploy"] == {"returncode": 0}
    assert json.loads(marker.read_text(encoding="utf-8"))["stats"]["window_days"] == 28
