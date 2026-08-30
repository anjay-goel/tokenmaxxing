from __future__ import annotations

import json
import shutil
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date
from importlib import resources
from pathlib import Path
from typing import TypeAlias
from urllib.parse import urljoin
from xml.sax.saxutils import escape as xml_escape

from jinja2 import ChoiceLoader, FileSystemLoader, PackageLoader, StrictUndefined
from jinja2.sandbox import SandboxedEnvironment
from markupsafe import Markup

from tokenmaxxing.presentation import api_value_text, compact_tokens
from tokenmaxxing.profile.config import ProfileConfig, ProfileInfo, SiteConfig
from tokenmaxxing.profile.data import ProfileData
from tokenmaxxing.profile.model_icons import model_icon
from tokenmaxxing.profile.project import ProfilePaths


JsonValue: TypeAlias = (
    None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
)
PROFILE_SOURCE_URL = "https://github.com/anjay-goel/tokenmaxxing"
_AGENT_SEGMENT_COUNT = 12

_HARNESS_ICONS = {
    "claude": "claude",
    "codex": "openai",
    "opencode": "opencode",
    "pi": "pi",
}
_AWARD_FACES = {
    "tokenmaxxer": "10B",
    "billion-day": "1B",
    "fleet-commander": "250",
    "hot-streak": "14",
    "model-collector": "10+",
    "all-systems-go": "ALL",
}


@dataclass(frozen=True, slots=True)
class RenderedSite:
    destination: Path
    files: tuple[Path, ...]


def _avatar_url(profile: ProfileInfo) -> str | None:
    if profile.avatar is None:
        return None
    suffix = profile.avatar.suffix.lower()
    if not suffix or not suffix[1:].isalnum() or len(suffix) > 9:
        suffix = ".image"
    return f"assets/avatar{suffix}"


def _public_profile(profile: ProfileInfo) -> dict[str, JsonValue]:
    return {
        "name": profile.name,
        "bio": profile.bio,
        "avatar": _avatar_url(profile),
        "links": [
            {"label": link.label, "value": link.value, "url": link.url}
            for link in profile.links
        ],
    }


def _public_site(site: SiteConfig) -> dict[str, JsonValue]:
    return {
        "title": site.title,
        "description": site.description,
        "canonical_url": site.canonical_url,
        "theme": site.theme,
        "accent": site.accent,
    }


def _display_model(name: str) -> str:
    if name.strip().lower() in {
        "(unknown)",
        "<unknown>",
        "unknown",
        "unknown model",
        "unknown models",
    }:
        return "unknown models"
    return name


def _provider_icon(item: object) -> str:
    provider = getattr(item, "provider", None)
    model = str(getattr(item, "model", ""))
    return model_icon(provider if isinstance(provider, str) else None, model)


def _group_token_models(models: object, total: int) -> list[dict[str, JsonValue]]:
    grouped: list[dict[str, JsonValue]] = []
    other = 0
    for model in models:
        tokens = int(getattr(model, "total_tokens"))
        if total > 0 and tokens * 100 < total * 5:
            other += tokens
            continue
        grouped.append(
            {"name": _display_model(str(getattr(model, "model"))), "tokens": tokens}
        )
    if other:
        grouped.append({"name": "other models", "tokens": other})
    return grouped


def _group_agent_models(models: object, total: int) -> list[dict[str, JsonValue]]:
    grouped: list[dict[str, JsonValue]] = []
    other = 0
    for model in models:
        agents = int(getattr(model, "agents"))
        if total > 0 and agents * 100 < total * 5:
            other += agents
            continue
        grouped.append(
            {"name": _display_model(str(getattr(model, "model"))), "agents": agents}
        )
    if other:
        grouped.append({"name": "other models", "agents": other})
    return grouped


def _agent_segments(data: ProfileData) -> dict[str, int]:
    totals: dict[str, int] = {}
    for daily in data.recent_days:
        for model in _group_agent_models(daily.models, daily.agents):
            name = str(model["name"])
            totals[name] = totals.get(name, 0) + int(model["agents"])
    return {
        name: index % _AGENT_SEGMENT_COUNT
        for index, (name, _) in enumerate(
            sorted(totals.items(), key=lambda item: (-item[1], item[0]))
        )
    }


def _activity_levels(data: ProfileData) -> dict[date, int]:
    positive = sorted(day.total_tokens for day in data.activity_days if day.total_tokens > 0)
    if not positive:
        return {}
    cuts = tuple(positive[min(len(positive) - 1, len(positive) * part // 4)] for part in (1, 2, 3))
    return {
        day.day: min(4, bisect_right(cuts, day.total_tokens) + 1)
        for day in data.activity_days
        if day.total_tokens > 0
    }


def _trend_view(data: ProfileData) -> dict[str, JsonValue]:
    width = 900
    top = 6
    baseline = 126
    window_days = (data.window_end - data.window_start).days + 1
    days = data.recent_token_days[-window_days:]
    maximum = max((day.total_tokens for day in days), default=0)
    step = width / (len(days) - 1) if len(days) > 1 else 0

    points: list[dict[str, JsonValue]] = []
    coordinates: list[tuple[float, float]] = []
    for index, day in enumerate(days):
        x = index * step if len(days) > 1 else width / 2
        ratio = day.total_tokens / maximum if maximum else 0
        y = baseline - ratio * (baseline - top)
        coordinates.append((x, y))
        points.append(
            {
                "date": day.day.isoformat(),
                "tokens": day.total_tokens,
                "models": _group_token_models(day.models, day.total_tokens),
            }
        )

    line_path = " ".join(
        f"{'M' if index == 0 else 'L'}{x:.2f} {y:.2f}"
        for index, (x, y) in enumerate(coordinates)
    )
    if coordinates:
        first_x = coordinates[0][0]
        last_x, last_y = coordinates[-1]
        area_path = f"{line_path} L{last_x:.2f} {baseline} L{first_x:.2f} {baseline} Z"
    else:
        last_x = width
        last_y = baseline
        area_path = ""

    return {
        "days": points,
        "line_path": line_path,
        "area_path": area_path,
        "last_x": round(last_x, 2),
        "last_y": round(last_y, 2),
    }


def _cards(config: ProfileConfig, data: ProfileData) -> list[dict[str, JsonValue]]:
    cards: list[dict[str, JsonValue]] = []
    api_value = (
        api_value_text(data.api_equivalent)
        if data.api_equivalent.total_tokens > 0
        else None
    )
    show_api_value = config.metrics.show_api_equivalent and api_value is not None
    if show_api_value:
        cards.append({"label": "API equivalent", "value": api_value})
    if config.metrics.show_agents:
        cards.append({"label": "Agents", "value": compact_tokens(data.agent_count)})
    if config.metrics.show_peak_usage:
        cards.append({"label": "Peak usage", "value": compact_tokens(data.peak_usage)})
    if config.metrics.show_longest_streak:
        cards.append({"label": "Longest streak", "value": f"{data.longest_streak}d"})
    if not show_api_value and config.metrics.show_models:
        cards.append({"label": "Models", "value": compact_tokens(data.model_count)})
    return cards[:4]


def _public_stats(config: ProfileConfig, data: ProfileData) -> dict[str, JsonValue]:
    levels = _activity_levels(data)
    activity = []
    for daily in data.activity_days:
        tracked = data.first_tracked_day is not None and daily.day >= data.first_tracked_day
        state = "active" if daily.total_tokens else "quiet" if tracked else "untracked"
        activity.append(
            {
                "date": daily.day.isoformat(),
                "tokens": daily.total_tokens,
                "state": state,
                "level": levels.get(daily.day, 0),
                "models": _group_token_models(daily.models, daily.total_tokens),
            }
        )
    agents = [
        {
            "date": daily.day.isoformat(),
            "agents": daily.agents,
            "models": _group_agent_models(daily.models, daily.agents),
        }
        for daily in data.recent_days
    ]
    models = [
        {
            "name": _display_model(model.model),
            "tokens": model.total_tokens,
            "icon": _provider_icon(model),
        }
        for model in data.models
    ]
    harnesses = [
        {
            "name": harness.harness,
            "tokens": harness.total_tokens,
            "icon": _HARNESS_ICONS.get(harness.harness.lower(), "generic"),
        }
        for harness in data.harnesses
    ]
    awards = [
        {
            "key": award.key,
            "name": award.name,
            "description": award.description,
            "metric_value": award.metric_value,
            "earned_on": award.earned_on.isoformat() if award.earned_on else None,
        }
        for award in getattr(data, "awards", ())
    ]
    return {
        "window_start": data.window_start.isoformat(),
        "window_end": data.window_end.isoformat(),
        "window_days": (data.window_end - data.window_start).days + 1,
        "total_tokens": data.total_tokens,
        "all_time_tokens": data.all_time_tokens,
        "quip": data.quip,
        "cards": _cards(config, data),
        "activity": activity,
        "agents": agents,
        "models": models,
        "harnesses": harnesses,
        "awards": awards,
    }


def _award_metric(key: str, value: int) -> str:
    if key in {"tokenmaxxer", "billion-day"}:
        return f"{compact_tokens(value)} tokens"
    if key == "fleet-commander":
        return f"{compact_tokens(value)} agents"
    if key == "hot-streak":
        return f"{value} days"
    if key == "model-collector":
        return f"{value} models"
    if key == "all-systems-go":
        return f"{value} harnesses"
    return compact_tokens(value)


def _award_views(data: ProfileData) -> list[dict[str, JsonValue]]:
    return [
        {
            "key": award.key,
            "modifier": award.key if award.key in _AWARD_FACES else "generic",
            "name": award.name,
            "description": award.description,
            "face": _AWARD_FACES.get(award.key, "★"),
            "metric": _award_metric(award.key, award.metric_value),
            "earned_on": award.earned_on.isoformat() if award.earned_on else None,
        }
        for award in getattr(data, "awards", ())
    ]


def public_payload(config: ProfileConfig, data: ProfileData) -> dict[str, JsonValue]:
    return {
        "schema_version": 1,
        "generated_at": data.generated_at.isoformat(),
        "profile": _public_profile(config.profile),
        "site": _public_site(config.site),
        "stats": _public_stats(config, data),
    }


def _environment(template_root: Path | None) -> SandboxedEnvironment:
    loaders = []
    if template_root is not None:
        loaders.append(FileSystemLoader(template_root))
    loaders.append(PackageLoader("tokenmaxxing.profile", "templates"))
    environment = SandboxedEnvironment(
        loader=ChoiceLoader(loaders),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    environment.filters["tokens"] = compact_tokens
    environment.filters["date"] = _format_date
    environment.filters["short_date"] = _format_short_date
    return environment


def _format_date(value: str) -> str:
    parsed = date.fromisoformat(value)
    return f"{parsed.strftime('%b')} {parsed.day}, {parsed.year}"


def _format_short_date(value: str) -> str:
    parsed = date.fromisoformat(value)
    return f"{parsed.strftime('%b')} {parsed.day}"


def _copy_assets(destination: Path) -> None:
    source = resources.files("tokenmaxxing.profile").joinpath("assets")
    with resources.as_file(source) as source_path:
        shutil.copytree(source_path, destination / "assets", dirs_exist_ok=True)


def _copy_project_assets(config: ProfileConfig, paths: ProfilePaths, destination: Path) -> None:
    avatar = config.profile.avatar
    if avatar is not None:
        if not avatar.is_file():
            raise FileNotFoundError(f"profile avatar does not exist: {avatar}")
        target = destination / str(_avatar_url(config.profile))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(avatar, target)
    custom_css = paths.root / "custom.css"
    if custom_css.is_file():
        shutil.copyfile(custom_css, destination / "assets" / "custom.css")


def _structured_data(payload: dict[str, JsonValue]) -> Markup:
    profile = payload["profile"]
    site = payload["site"]
    document = {
        "@context": "https://schema.org",
        "@type": "ProfilePage",
        "url": site["canonical_url"],
        "name": site["title"],
        "description": site["description"],
        "mainEntity": {
            "@type": "Person",
            "name": profile["name"],
            "description": profile["bio"],
        },
    }
    encoded = json.dumps(document, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    return Markup(encoded)


def _write_discovery(config: ProfileConfig, destination: Path, noindex: bool) -> None:
    if noindex:
        (destination / "robots.txt").write_text(
            "User-agent: *\nAllow: /\n", encoding="utf-8"
        )
        sitemap = destination / "sitemap.xml"
        if sitemap.exists():
            sitemap.unlink()
        return
    sitemap_url = urljoin(config.site.canonical_url, "sitemap.xml")
    (destination / "robots.txt").write_text(
        f"User-agent: *\nAllow: /\nSitemap: {sitemap_url}\n", encoding="utf-8"
    )
    canonical = xml_escape(config.site.canonical_url)
    (destination / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<url><loc>{canonical}</loc></url></urlset>\n",
        encoding="utf-8",
    )


def render_site(
    config: ProfileConfig,
    data: ProfileData,
    paths: ProfilePaths,
    destination: Path,
    *,
    noindex: bool,
) -> RenderedSite:
    if config.profile.avatar is not None and not config.profile.avatar.is_file():
        raise FileNotFoundError(f"profile avatar does not exist: {config.profile.avatar}")
    destination.mkdir(parents=True, exist_ok=True)
    _copy_assets(destination)
    _copy_project_assets(config, paths, destination)

    payload = public_payload(config, data)
    effective_noindex = noindex or not config.site.indexable
    template_root = paths.root / "template"
    template = _environment(template_root if template_root.is_dir() else None).get_template(
        "index.html.j2"
    )
    html = template.render(
        profile=payload["profile"],
        site=payload["site"],
        stats=payload["stats"],
        trend=_trend_view(data),
        asset_version=int(data.generated_at.timestamp()),
        activity_tokens=compact_tokens(
            sum(day.total_tokens for day in data.activity_days)
        ),
        agent_segments=_agent_segments(data),
        awards=_award_views(data),
        noindex=effective_noindex,
        structured_data=_structured_data(payload),
        has_custom_css=(paths.root / "custom.css").is_file(),
        source_url=PROFILE_SOURCE_URL,
    )
    (destination / "index.html").write_text(html, encoding="utf-8")
    (destination / "profile.json").write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    _write_discovery(config, destination, effective_noindex)
    files = tuple(sorted(path for path in destination.rglob("*") if path.is_file()))
    return RenderedSite(destination=destination, files=files)
