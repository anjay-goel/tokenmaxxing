from __future__ import annotations

import os
import re
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time
from importlib import resources
from pathlib import Path
from string import Formatter
from typing import Literal, cast
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml


_SCHEDULE_TIME = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d\Z")
_THEMES = frozenset({"auto", "light", "dark"})
_DEPLOY_PLACEHOLDERS = frozenset({"site_dir"})
_MISSING = object()


@dataclass(frozen=True, slots=True)
class ProfileLink:
    label: str
    value: str
    url: str


@dataclass(frozen=True, slots=True)
class ProfileInfo:
    name: str
    bio: str = ""
    avatar: Path | None = None
    links: tuple[ProfileLink, ...] = ()


@dataclass(frozen=True, slots=True)
class SiteConfig:
    title: str
    description: str
    canonical_url: str
    indexable: bool
    timezone: ZoneInfo
    theme: Literal["auto", "light", "dark"] = "auto"
    accent: str = "violet"


@dataclass(frozen=True, slots=True)
class MetricsConfig:
    window_days: int = 28
    show_api_equivalent: bool = True
    show_agents: bool = True
    show_peak_usage: bool = True
    show_longest_streak: bool = True
    show_models: bool = True


@dataclass(frozen=True, slots=True)
class DeployConfig:
    command: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ScheduleConfig:
    time: time = time(hour=9)


@dataclass(frozen=True, slots=True)
class ProfileConfig:
    version: Literal[1]
    profile: ProfileInfo
    site: SiteConfig
    metrics: MetricsConfig = MetricsConfig()
    deploy: DeployConfig = DeployConfig()
    schedule: ScheduleConfig = ScheduleConfig()


def _dotted(path: str, key: object) -> str:
    return f"{path}.{key}" if path else str(key)


def _expect_mapping(value: object, path: str) -> Mapping[object, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must be a mapping")
    return value


def _expect_keys(
    value: Mapping[object, object], allowed: frozenset[str], path: str
) -> None:
    for key in value:
        if not isinstance(key, str) or key not in allowed:
            raise ValueError(f"unknown configuration key: {_dotted(path, key)}")


def _value(mapping: Mapping[object, object], key: str) -> object:
    return mapping[key] if key in mapping else _MISSING


def _string(
    value: object,
    path: str,
    *,
    default: str | object = _MISSING,
    allow_empty: bool = False,
) -> str:
    if value is _MISSING and default is not _MISSING:
        return cast(str, default)
    if not isinstance(value, str) or (not allow_empty and not value):
        qualifier = "a string" if allow_empty else "a non-empty string"
        raise ValueError(f"{path} must be {qualifier}")
    return value


def _boolean(
    value: object, path: str, *, default: bool | object = _MISSING
) -> bool:
    if value is _MISSING and default is not _MISSING:
        return cast(bool, default)
    if not isinstance(value, bool):
        raise ValueError(f"{path} must be a boolean")
    return value


def _positive_integer(value: object, path: str, *, default: int) -> int:
    if value is _MISSING:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{path} must be a positive integer")
    return value


def _path_inside_project(value: str, project: Path, path: str) -> Path:
    project = project.resolve()
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = project / candidate
    candidate = candidate.resolve()
    if not candidate.is_relative_to(project):
        raise ValueError(f"{path} must stay inside the profile project")
    return candidate


def normalize_canonical_url(value: str, path: str = "site.canonical_url") -> str:
    parsed = urlsplit(value)
    try:
        parsed.port
    except ValueError as error:
        raise ValueError(f"{path} must be an absolute https URL") from error
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(f"{path} must use https with an absolute host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{path} must not include credentials")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{path} must not include a query or fragment")
    normalized_path = parsed.path or "/"
    if not normalized_path.endswith("/"):
        normalized_path += "/"
    return urlunsplit(("https", parsed.netloc, normalized_path, "", ""))


def validate_public_link_url(
    value: str, path: str = "profile link URL"
) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"https", "mailto"}:
        raise ValueError(f"{path} must use https or mailto")
    if parsed.scheme == "https" and not parsed.netloc:
        raise ValueError(f"{path} must be an absolute https URL")
    if parsed.scheme == "mailto" and not parsed.path:
        raise ValueError(f"{path} must include an address")
    return value


def _section(
    root: Mapping[object, object],
    key: str,
    allowed: frozenset[str],
    *,
    required: bool,
) -> Mapping[object, object]:
    value = _value(root, key)
    if value is _MISSING and not required:
        value = {}
    section = _expect_mapping(value, key)
    _expect_keys(section, allowed, key)
    return section


def _profile_info(raw: Mapping[object, object], project: Path) -> ProfileInfo:
    avatar_value = _value(raw, "avatar")
    avatar = (
        None
        if avatar_value is _MISSING or avatar_value is None
        else _path_inside_project(
            _string(avatar_value, "profile.avatar"), project, "profile.avatar"
        )
    )
    links_value = _value(raw, "links")
    if links_value is _MISSING:
        links_value = []
    if not isinstance(links_value, list):
        raise ValueError("profile.links must be a list")
    links: list[ProfileLink] = []
    for index, link_value in enumerate(links_value):
        link_path = f"profile.links[{index}]"
        link = _expect_mapping(link_value, link_path)
        _expect_keys(link, frozenset({"label", "value", "url"}), link_path)
        url_path = f"{link_path}.url"
        url = _string(_value(link, "url"), url_path)
        url = validate_public_link_url(url, url_path)
        links.append(
            ProfileLink(
                label=_string(_value(link, "label"), f"{link_path}.label"),
                value=_string(_value(link, "value"), f"{link_path}.value"),
                url=url,
            )
        )
    return ProfileInfo(
        name=_string(_value(raw, "name"), "profile.name"),
        bio=_string(
            _value(raw, "bio"), "profile.bio", default="", allow_empty=True
        ),
        avatar=avatar,
        links=tuple(links),
    )


def _site_config(raw: Mapping[object, object]) -> SiteConfig:
    timezone_name = _string(_value(raw, "timezone"), "site.timezone")
    try:
        timezone = ZoneInfo(timezone_name)
    except (ValueError, ZoneInfoNotFoundError) as error:
        raise ValueError("site.timezone must be an IANA timezone name") from error
    theme = _string(_value(raw, "theme"), "site.theme", default="auto")
    if theme not in _THEMES:
        raise ValueError("site.theme must be auto, light, or dark")
    return SiteConfig(
        title=_string(_value(raw, "title"), "site.title"),
        description=_string(_value(raw, "description"), "site.description"),
        canonical_url=normalize_canonical_url(
            _string(_value(raw, "canonical_url"), "site.canonical_url")
        ),
        indexable=_boolean(_value(raw, "indexable"), "site.indexable"),
        timezone=timezone,
        theme=cast(Literal["auto", "light", "dark"], theme),
        accent=_string(_value(raw, "accent"), "site.accent", default="violet"),
    )


def _metrics_config(raw: Mapping[object, object]) -> MetricsConfig:
    return MetricsConfig(
        window_days=_positive_integer(
            _value(raw, "window_days"), "metrics.window_days", default=28
        ),
        show_api_equivalent=_boolean(
            _value(raw, "show_api_equivalent"),
            "metrics.show_api_equivalent",
            default=True,
        ),
        show_agents=_boolean(
            _value(raw, "show_agents"), "metrics.show_agents", default=True
        ),
        show_peak_usage=_boolean(
            _value(raw, "show_peak_usage"), "metrics.show_peak_usage", default=True
        ),
        show_longest_streak=_boolean(
            _value(raw, "show_longest_streak"),
            "metrics.show_longest_streak",
            default=True,
        ),
        show_models=_boolean(
            _value(raw, "show_models"), "metrics.show_models", default=True
        ),
    )


def _deploy_config(raw: Mapping[object, object]) -> DeployConfig:
    command_value = _value(raw, "command")
    if command_value is _MISSING:
        command_value = []
    if not isinstance(command_value, list):
        raise ValueError("deploy.command must be a list of arguments")
    command: list[str] = []
    for index, argument_value in enumerate(command_value):
        path = f"deploy.command[{index}]"
        argument = _string(argument_value, path)
        try:
            fields = tuple(Formatter().parse(argument))
        except ValueError as error:
            raise ValueError(f"{path} contains an invalid placeholder") from error
        for _, field_name, format_spec, conversion in fields:
            if field_name is None:
                continue
            if (
                field_name not in _DEPLOY_PLACEHOLDERS
                or format_spec
                or conversion is not None
            ):
                raise ValueError(f"{path} contains an unknown placeholder")
        command.append(argument)
    return DeployConfig(command=tuple(command))


def _schedule_config(raw: Mapping[object, object]) -> ScheduleConfig:
    value = _string(_value(raw, "time"), "schedule.time", default="09:00")
    if _SCHEDULE_TIME.fullmatch(value) is None:
        raise ValueError("schedule.time must use HH:MM in 24-hour time")
    hour, minute = map(int, value.split(":"))
    return ScheduleConfig(time=time(hour=hour, minute=minute))


def _remembered_profile_error(path: Path) -> FileNotFoundError:
    return FileNotFoundError(
        f"remembered profile is unavailable: {path}; "
        "run `tokenmaxxing profile init` or pass --config"
    )


def remembered_config(path: Path) -> Path | None:
    if not path.exists():
        return None
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise _remembered_profile_error(path) from error
    candidate = Path(value) if value else path
    if not value or not candidate.is_absolute() or not candidate.is_file():
        raise _remembered_profile_error(candidate)
    return candidate.resolve()


def remember_config(config_path: Path, path: Path) -> None:
    config_path = config_path.resolve(strict=True)
    if not config_path.is_file():
        raise FileNotFoundError(f"profile configuration is not a file: {config_path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as output:
            temporary = Path(output.name)
            output.write(f"{config_path}\n")
            output.flush()
            os.fsync(output.fileno())
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            if sys.platform != "win32":
                raise
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def discover_config(start: Path, remembered: Path | None = None) -> Path:
    current = start.resolve()
    if current.is_file():
        current = current.parent
    for directory in (current, *current.parents):
        candidate = directory / "config.yaml"
        if candidate.is_file():
            return candidate
    if remembered is not None:
        if config := remembered_config(remembered):
            return config
    raise FileNotFoundError(
        "could not find config.yaml in this directory or its parents"
    )


def _config_from_document(loaded: object, path: Path) -> ProfileConfig:
    root = _expect_mapping(loaded, "config")
    _expect_keys(
        root,
        frozenset({"version", "profile", "site", "metrics", "deploy", "schedule"}),
        "",
    )
    profile = _section(
        root,
        "profile",
        frozenset({"name", "bio", "avatar", "links"}),
        required=True,
    )
    site = _section(
        root,
        "site",
        frozenset(
            {
                "title",
                "description",
                "canonical_url",
                "indexable",
                "timezone",
                "theme",
                "accent",
            }
        ),
        required=True,
    )
    metrics = _section(
        root,
        "metrics",
        frozenset(
            {
                "window_days",
                "show_api_equivalent",
                "show_agents",
                "show_peak_usage",
                "show_longest_streak",
                "show_models",
            }
        ),
        required=False,
    )
    deploy = _section(
        root, "deploy", frozenset({"command"}), required=False
    )
    schedule = _section(
        root, "schedule", frozenset({"time"}), required=False
    )
    version = _value(root, "version")
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise ValueError("version must be 1")
    return ProfileConfig(
        version=1,
        profile=_profile_info(profile, path.resolve().parent),
        site=_site_config(site),
        metrics=_metrics_config(metrics),
        deploy=_deploy_config(deploy),
        schedule=_schedule_config(schedule),
    )


def load_config(path: Path) -> ProfileConfig:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ValueError("config.yaml contains invalid YAML") from error
    return _config_from_document(loaded, path)


def load_starter_config(path: Path) -> ProfileConfig:
    source = resources.files("tokenmaxxing.profile").joinpath(
        "starters", "config.yaml"
    )
    try:
        loaded = yaml.safe_load(source.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ValueError("packaged starter contains invalid YAML") from error
    return _config_from_document(loaded, path)


def _avatar_value(config_path: Path, avatar: Path | None) -> str | None:
    if avatar is None:
        return None
    resolved = _path_inside_project(
        str(avatar), config_path.resolve().parent, "profile.avatar"
    )
    return resolved.relative_to(config_path.resolve().parent).as_posix()


def _config_document(path: Path, config: ProfileConfig) -> dict[str, object]:
    profile: dict[str, object] = {
        "name": config.profile.name,
        "bio": config.profile.bio,
    }
    if avatar := _avatar_value(path, config.profile.avatar):
        profile["avatar"] = avatar
    profile["links"] = [
        {"label": link.label, "value": link.value, "url": link.url}
        for link in config.profile.links
    ]
    return {
        "version": config.version,
        "profile": profile,
        "site": {
            "title": config.site.title,
            "description": config.site.description,
            "canonical_url": config.site.canonical_url,
            "indexable": config.site.indexable,
            "timezone": config.site.timezone.key,
            "theme": config.site.theme,
            "accent": config.site.accent,
        },
        "metrics": {
            "window_days": config.metrics.window_days,
            "show_api_equivalent": config.metrics.show_api_equivalent,
            "show_agents": config.metrics.show_agents,
            "show_peak_usage": config.metrics.show_peak_usage,
            "show_longest_streak": config.metrics.show_longest_streak,
            "show_models": config.metrics.show_models,
        },
        "deploy": {"command": list(config.deploy.command)},
        "schedule": {"time": config.schedule.time.strftime("%H:%M")},
    }


def validate_config(path: Path, config: ProfileConfig) -> ProfileConfig:
    return _config_from_document(_config_document(path, config), path)


def write_initial_config(path: Path, config: ProfileConfig) -> None:
    data = _config_document(path, validate_config(path, config))
    path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
