from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import stat
import tempfile
import warnings
from dataclasses import dataclass
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TypeAlias
from urllib.parse import unquote, urljoin, urlsplit
from uuid import uuid4
from xml.etree import ElementTree

from tokenmaxxing.db import Database
from tokenmaxxing.profile.config import load_config
from tokenmaxxing.profile.data import build_profile_data
from tokenmaxxing.profile.project import profile_paths
from tokenmaxxing.profile.render import PROFILE_SOURCE_URL, render_site
from tokenmaxxing.repository import Repository


JsonValue: TypeAlias = (
    None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
)

_REQUIRED_FILES = (
    "index.html",
    "profile.json",
    "robots.txt",
    "assets/profile.css",
    "assets/profile.js",
)
_FORBIDDEN_FIELD_PARTS = (
    "session",
    "run_id",
    "event",
    "prompt",
    "reasoning",
    "path",
    "artifact",
)
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_CSS_URL = re.compile(
    r"(?<![\w-])url\s*\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE
)
_CSS_URL_START = re.compile(r"(?i)(?<![\w-])url\s*\(")
_CSS_NON_URL_RESOURCE = re.compile(
    r"(?i)@import\b|(?<![\w-])(?:-webkit-)?image-set\s*\(|(?<![\w-])src\s*\("
)
_CSS_ANY_RESOURCE = re.compile(
    r"(?i)@import\b|(?<![\w-])url\s*\(|"
    r"(?<![\w-])(?:-webkit-)?image-set\s*\(|(?<![\w-])src\s*\("
)
_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_REMOTE_REFERENCE = re.compile(r"(?i)(?:https?:)?//")
_REMOTE_EXECUTABLE = re.compile(r"(?i)https?://|['\"]\s*//")
_NETWORK_JAVASCRIPT = re.compile(
    r"(?i)\b(?:fetch|XMLHttpRequest|WebSocket|EventSource|sendBeacon|importScripts)\b|"
    r"\bimport\b"
)
_JAVASCRIPT_BUDGET = 15_000
_DOCUMENT_BUDGET = 60_000
_OWNERSHIP_MARKER = ".tokenmaxxing-profile.json"
_OWNERSHIP_PAYLOAD: dict[str, JsonValue] = {
    "kind": "tokenmaxxing-profile",
    "schema_version": 1,
}


class BuildRecoveryError(RuntimeError):
    pass


class BuildCleanupWarning(UserWarning):
    pass


@dataclass(frozen=True, slots=True)
class BuildResult:
    site_dir: Path
    generated_at: datetime
    file_count: int
    total_bytes: int


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.asset_references: list[str] = []
        self.link_references: list[str] = []
        self.canonicals: list[str] = []
        self.robots: list[str] = []
        self.total_tokens: list[str] = []
        self.all_time_tokens: list[str] = []
        self.navigation_references: list[str] = []
        self.inline_styles: list[str] = []
        self.inline_scripts: list[tuple[str, str]] = []
        self.event_handler_attributes: list[str] = []
        self.srcdoc_attributes = 0
        self.base_elements = 0
        self.refresh_elements = 0
        self._style_depth = 0
        self._script_depth = 0
        self._script_type = ""
        self._script_parts: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        for name, _ in attrs:
            lowered = name.lower()
            if lowered.startswith("on"):
                self.event_handler_attributes.append(lowered)
            if lowered == "srcdoc":
                self.srcdoc_attributes += 1
        values = {name.lower(): value or "" for name, value in attrs}
        tag = tag.lower()
        if tag == "base":
            self.base_elements += 1
        if tag == "style":
            self._style_depth += 1
        if tag == "script" and not values.get("src"):
            self._script_depth += 1
            if self._script_depth == 1:
                self._script_type = values.get("type", "").strip().lower()
                self._script_parts = []
        if values.get("style"):
            self.inline_styles.append(values["style"])
        if tag == "meta" and values.get("name", "").lower() == "robots":
            self.robots.append(values.get("content", ""))
        if tag == "meta" and values.get("http-equiv", "").lower() == "refresh":
            self.refresh_elements += 1
        if tag == "meta" and (
            values.get("property", "").lower() in {"og:image", "og:video", "og:audio"}
            or values.get("name", "").lower()
            in {"twitter:image", "twitter:player", "msapplication-tileimage"}
        ):
            if values.get("content"):
                self.asset_references.append(values["content"])
        if tag == "link":
            relations = set(values.get("rel", "").lower().split())
            href = values.get("href")
            if "canonical" in relations and href:
                self.canonicals.append(href)
            elif href:
                self.asset_references.append(href)
        if tag == "a" and values.get("href"):
            self.navigation_references.append(values["href"])
        if tag == "form" and values.get("action"):
            self.navigation_references.append(values["action"])
        if tag in {"button", "input"} and values.get("formaction"):
            self.navigation_references.append(values["formaction"])
        for attribute in ("data-total-tokens", "data-all-time-tokens"):
            if attribute not in values:
                continue
            target = (
                self.total_tokens
                if attribute == "data-total-tokens"
                else self.all_time_tokens
            )
            target.append(values[attribute])
        for attribute in {
            "script": ("src",),
            "img": ("src",),
            "source": ("src",),
            "audio": ("src",),
            "video": ("src", "poster"),
            "iframe": ("src",),
            "embed": ("src",),
            "object": ("data",),
            "input": ("src",),
            "track": ("src",),
            "image": ("href", "xlink:href"),
            "use": ("href", "xlink:href"),
        }.get(tag, ()):
            if values.get(attribute):
                self.asset_references.append(values[attribute])
        if tag in {"img", "source"} and values.get("srcset"):
            self.asset_references.extend(
                item.strip().split()[0]
                for item in values["srcset"].split(",")
                if item.strip()
            )

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "style" and self._style_depth:
            self._style_depth -= 1
        if tag.lower() == "script" and self._script_depth:
            self._script_depth -= 1
            if self._script_depth == 0:
                self.inline_scripts.append(
                    (self._script_type, "".join(self._script_parts))
                )
                self._script_type = ""
                self._script_parts = []

    def handle_data(self, data: str) -> None:
        if self._style_depth:
            self.inline_styles.append(data)
        if self._script_depth:
            self._script_parts.append(data)


def _strict_json(text: str, label: str) -> JsonValue:
    def object_from_pairs(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{label} must not contain duplicate keys")
            result[key] = value
        return result

    def reject_constant(value: str) -> JsonValue:
        raise ValueError(f"{label} must not contain {value}")

    try:
        return json.loads(
            text,
            object_pairs_hook=object_from_pairs,
            parse_constant=reject_constant,
        )
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
        if isinstance(error, ValueError) and str(error).startswith(label):
            raise
        raise ValueError(f"{label} must contain strict JSON") from error


def _expect_object(
    value: JsonValue,
    path: str,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be an object")
    missing = required - value.keys()
    if missing:
        raise ValueError(f"missing public field: {path}.{min(missing)}")
    unknown = value.keys() - required - optional
    if unknown:
        raise ValueError(f"unknown public field: {path}.{min(unknown)}")
    return value


def _expect_list(value: JsonValue, path: str) -> list[JsonValue]:
    if not isinstance(value, list):
        raise ValueError(f"{path} must be a list")
    return value


def _expect_string(value: JsonValue, path: str, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, str):
        raise ValueError(f"{path} must be a string")


def _expect_integer(value: JsonValue, path: str, *, positive: bool = False) -> None:
    minimum = 1 if positive else 0
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{path} must be an integer of at least {minimum}")


def _expect_date(value: JsonValue, path: str, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    _expect_string(value, path)
    if _ISO_DATE.fullmatch(value) is None:  # type: ignore[arg-type]
        raise ValueError(f"{path} must be an ISO date")
    try:
        date.fromisoformat(value)  # type: ignore[arg-type]
    except ValueError as error:
        raise ValueError(f"{path} must be an ISO date") from error


def _reject_forbidden_fields(value: JsonValue, path: str = "profile.json") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            lowered = key.lower()
            if any(part in lowered for part in _FORBIDDEN_FIELD_PARTS):
                raise ValueError(f"forbidden public field: {path}.{key}")
            _reject_forbidden_fields(nested, f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_forbidden_fields(nested, f"{path}[{index}]")


def _is_private_path(value: str) -> bool:
    lowered = value.lower()
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    return (
        lowered.startswith(("file:", "~/", "~\\"))
        or posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or "\\" in value
        or ".." in posix.parts
    )


def _reject_local_paths(
    value: JsonValue, path: str = "profile.json", *, field: str | None = None
) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            _reject_local_paths(nested, f"{path}.{key}", field=key)
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_local_paths(nested, f"{path}[{index}]", field=field)
    elif isinstance(value, str) and field not in {"url", "canonical_url"}:
        if _is_private_path(value):
            raise ValueError(f"local path leaked into public output: {path}")


def _expect_public_url(value: JsonValue, path: str) -> None:
    _expect_string(value, path)
    parsed = urlsplit(value)  # type: ignore[arg-type]
    if parsed.scheme == "https":
        if not parsed.netloc:
            raise ValueError(f"{path} must be an absolute https URL")
        return
    if parsed.scheme == "mailto":
        if not parsed.path:
            raise ValueError(f"{path} must include an address")
        return
    raise ValueError(f"{path} must use https or mailto")


def _validate_named_token_models(value: JsonValue, path: str) -> None:
    for index, item in enumerate(_expect_list(value, path)):
        item_path = f"{path}[{index}]"
        model = _expect_object(item, item_path, frozenset({"name", "tokens"}))
        _expect_string(model["name"], f"{item_path}.name")
        _expect_integer(model["tokens"], f"{item_path}.tokens")


def _validate_named_agent_models(value: JsonValue, path: str) -> None:
    for index, item in enumerate(_expect_list(value, path)):
        item_path = f"{path}[{index}]"
        model = _expect_object(item, item_path, frozenset({"name", "agents"}))
        _expect_string(model["name"], f"{item_path}.name")
        _expect_integer(model["agents"], f"{item_path}.agents")


def _validate_public_payload(payload: JsonValue) -> dict[str, JsonValue]:
    _reject_forbidden_fields(payload)
    _reject_local_paths(payload)
    root = _expect_object(
        payload,
        "profile.json",
        frozenset({"schema_version", "generated_at", "profile", "site", "stats"}),
    )
    _expect_integer(root["schema_version"], "profile.json.schema_version", positive=True)
    if root["schema_version"] != 1:
        raise ValueError("profile.json schema_version must be 1")
    _expect_string(root["generated_at"], "profile.json.generated_at")
    try:
        generated_at = datetime.fromisoformat(root["generated_at"])  # type: ignore[arg-type]
    except ValueError as error:
        raise ValueError("profile.json.generated_at must be an ISO datetime") from error
    if generated_at.utcoffset() is None:
        raise ValueError("profile.json.generated_at must include a timezone offset")

    profile = _expect_object(
        root["profile"],
        "profile.json.profile",
        frozenset({"name", "bio", "avatar", "links"}),
    )
    for key in ("name", "bio"):
        _expect_string(profile[key], f"profile.json.profile.{key}")
    _expect_string(profile["avatar"], "profile.json.profile.avatar", nullable=True)
    for index, item in enumerate(_expect_list(profile["links"], "profile.json.profile.links")):
        item_path = f"profile.json.profile.links[{index}]"
        link = _expect_object(
            item, item_path, frozenset({"label", "value", "url"})
        )
        for key in ("label", "value", "url"):
            _expect_string(link[key], f"{item_path}.{key}")
        _expect_public_url(link["url"], f"{item_path}.url")

    site = _expect_object(
        root["site"],
        "profile.json.site",
        frozenset({"title", "description", "canonical_url", "theme", "accent"}),
    )
    for key in site:
        _expect_string(site[key], f"profile.json.site.{key}")
    canonical = urlsplit(site["canonical_url"])  # type: ignore[arg-type]
    if canonical.scheme != "https" or not canonical.netloc:
        raise ValueError("profile.json.site.canonical_url must be an absolute https URL")

    stats = _expect_object(
        root["stats"],
        "profile.json.stats",
        frozenset(
            {
                "window_start",
                "window_end",
                "window_days",
                "total_tokens",
                "all_time_tokens",
                "quip",
                "cards",
                "activity",
                "agents",
                "models",
                "harnesses",
            }
        ),
        frozenset({"awards"}),
    )
    _expect_date(stats["window_start"], "profile.json.stats.window_start")
    _expect_date(stats["window_end"], "profile.json.stats.window_end")
    _expect_integer(stats["window_days"], "profile.json.stats.window_days", positive=True)
    _expect_integer(stats["total_tokens"], "profile.json.stats.total_tokens")
    _expect_integer(stats["all_time_tokens"], "profile.json.stats.all_time_tokens")
    _expect_string(stats["quip"], "profile.json.stats.quip")
    for index, item in enumerate(_expect_list(stats["cards"], "profile.json.stats.cards")):
        item_path = f"profile.json.stats.cards[{index}]"
        card = _expect_object(item, item_path, frozenset({"label", "value"}))
        _expect_string(card["label"], f"{item_path}.label")
        _expect_string(card["value"], f"{item_path}.value")
    for index, item in enumerate(_expect_list(stats["activity"], "profile.json.stats.activity")):
        item_path = f"profile.json.stats.activity[{index}]"
        daily = _expect_object(
            item,
            item_path,
            frozenset({"date", "tokens", "state", "level", "models"}),
        )
        _expect_date(daily["date"], f"{item_path}.date")
        _expect_integer(daily["tokens"], f"{item_path}.tokens")
        if daily["state"] not in {"active", "quiet", "untracked"}:
            raise ValueError(f"{item_path}.state is invalid")
        _expect_integer(daily["level"], f"{item_path}.level")
        if daily["level"] > 4:  # type: ignore[operator]
            raise ValueError(f"{item_path}.level must be at most 4")
        _validate_named_token_models(daily["models"], f"{item_path}.models")
    for index, item in enumerate(_expect_list(stats["agents"], "profile.json.stats.agents")):
        item_path = f"profile.json.stats.agents[{index}]"
        daily = _expect_object(
            item, item_path, frozenset({"date", "agents", "models"})
        )
        _expect_date(daily["date"], f"{item_path}.date")
        _expect_integer(daily["agents"], f"{item_path}.agents")
        _validate_named_agent_models(daily["models"], f"{item_path}.models")
    for group in ("models", "harnesses"):
        for index, item in enumerate(_expect_list(stats[group], f"profile.json.stats.{group}")):
            item_path = f"profile.json.stats.{group}[{index}]"
            ranking = _expect_object(
                item, item_path, frozenset({"name", "tokens", "icon"})
            )
            _expect_string(ranking["name"], f"{item_path}.name")
            _expect_integer(ranking["tokens"], f"{item_path}.tokens")
            _expect_string(ranking["icon"], f"{item_path}.icon")
    for index, item in enumerate(_expect_list(stats.get("awards", []), "profile.json.stats.awards")):
        item_path = f"profile.json.stats.awards[{index}]"
        award = _expect_object(
            item,
            item_path,
            frozenset({"key", "name", "description", "metric_value", "earned_on"}),
        )
        for key in ("key", "name", "description"):
            _expect_string(award[key], f"{item_path}.{key}")
        _expect_integer(award["metric_value"], f"{item_path}.metric_value")
        _expect_date(award["earned_on"], f"{item_path}.earned_on", nullable=True)
    return root


def _site_entries(site_dir: Path) -> tuple[list[Path], list[Path]]:
    try:
        root_mode = site_dir.lstat().st_mode
    except FileNotFoundError as error:
        raise ValueError(f"site directory does not exist: {site_dir}") from error
    if stat.S_ISLNK(root_mode) or not stat.S_ISDIR(root_mode):
        raise ValueError("site directory must be a real directory, not a symlink")
    directories = [site_dir]
    files: list[Path] = []
    pending = [site_dir]
    while pending:
        directory = pending.pop()
        for child in directory.iterdir():
            mode = child.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise ValueError(f"generated site must not contain a symlink: {child}")
            if stat.S_ISDIR(mode):
                directories.append(child)
                pending.append(child)
            elif stat.S_ISREG(mode):
                files.append(child)
            else:
                raise ValueError(f"generated site contains a non-regular file: {child}")
    return directories, files


def _local_reference(site_dir: Path, source: Path, reference: str, label: str) -> None:
    if reference.startswith("#"):
        return
    parsed = urlsplit(reference)
    if parsed.scheme or parsed.netloc or reference.startswith("//"):
        raise ValueError(f"remote asset is not allowed: {reference}")
    decoded = unquote(parsed.path)
    if not decoded or decoded.startswith(("/", "\\")) or "\\" in decoded:
        raise ValueError(f"{label} is not a safe relative path: {reference}")
    candidate = (source.parent / decoded).resolve(strict=False)
    root = site_dir.resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"{label} escapes site: {reference}")
    if not candidate.is_file():
        raise ValueError(f"missing local asset: {reference}")


def _allowed_public_urls(payload: dict[str, JsonValue]) -> frozenset[str]:
    profile = payload["profile"]
    site = payload["site"]
    assert isinstance(profile, dict) and isinstance(site, dict)
    links = profile["links"]
    assert isinstance(links, list)
    return frozenset(
        [site["canonical_url"], PROFILE_SOURCE_URL]
        + [link["url"] for link in links if isinstance(link, dict)]
    )  # type: ignore[list-item]


def _validate_navigation(
    site_dir: Path,
    index: Path,
    reference: str,
    allowed_urls: frozenset[str],
) -> None:
    if reference.startswith("#"):
        return
    parsed = urlsplit(reference)
    if parsed.scheme or parsed.netloc or reference.startswith("//"):
        if reference not in allowed_urls:
            raise ValueError(
                f"external navigation must match a configured public URL: {reference}"
            )
        return
    _local_reference(site_dir, index, reference, "HTML navigation reference")


def _validate_json_ld(value: JsonValue) -> None:
    if not isinstance(value, (dict, list)):
        raise ValueError("JSON-LD must be an object or list")
    _reject_forbidden_fields(value, "JSON-LD")
    _reject_local_paths(value, "JSON-LD")

    url_fields = {"@context", "url", "sameAs", "image"}

    def validate_urls(nested: JsonValue, path: str) -> None:
        if isinstance(nested, dict):
            for key, item in nested.items():
                item_path = f"{path}.{key}"
                if key in url_fields:
                    values = item if isinstance(item, list) else [item]
                    if not values:
                        raise ValueError(f"{item_path} must contain an HTTPS URL")
                    for candidate in values:
                        if not isinstance(candidate, str):
                            raise ValueError(f"{item_path} must contain HTTPS URLs")
                        parsed = urlsplit(candidate)
                        if parsed.scheme != "https" or not parsed.netloc:
                            raise ValueError(f"{item_path} must contain HTTPS URLs")
                else:
                    validate_urls(item, item_path)
        elif isinstance(nested, list):
            for index, item in enumerate(nested):
                validate_urls(item, f"{path}[{index}]")

    validate_urls(value, "JSON-LD")


def _validate_html(
    site_dir: Path, payload: dict[str, JsonValue], *, noindex: bool
) -> None:
    index = site_dir / "index.html"
    parser = _PageParser()
    parser.feed(index.read_text(encoding="utf-8"))
    if parser.event_handler_attributes:
        attribute = parser.event_handler_attributes[0]
        raise ValueError(
            f"HTML event-handler attributes beginning with 'on' are not allowed: {attribute}"
        )
    if parser.srcdoc_attributes:
        raise ValueError("HTML srcdoc attributes are not allowed")
    if parser.base_elements:
        raise ValueError("HTML base elements are not allowed")
    if parser.refresh_elements:
        raise ValueError("HTML refresh redirects are not allowed")
    for reference in parser.asset_references:
        _local_reference(site_dir, index, reference, "HTML asset reference")
    allowed_urls = _allowed_public_urls(payload)
    for reference in parser.navigation_references:
        _validate_navigation(site_dir, index, reference, allowed_urls)
    for css in parser.inline_styles:
        _validate_css_text(site_dir, index, css, label="inline CSS asset")
    for script_type, script in parser.inline_scripts:
        if script_type == "application/ld+json":
            _validate_json_ld(_strict_json(script, "JSON-LD"))
        else:
            raise ValueError(
                "inline executable scripts are not allowed; "
                "only application/ld+json data is permitted"
            )

    stats = payload["stats"]
    assert isinstance(stats, dict)
    expected_total = str(stats["total_tokens"])
    expected_all_time = str(stats["all_time_tokens"])
    if parser.total_tokens != [expected_total] or parser.all_time_tokens != [expected_all_time]:
        raise ValueError("visible total attributes do not match profile.json")

    robots_values = {value.lower().replace(" ", "") for value in parser.robots}
    site = payload["site"]
    assert isinstance(site, dict)
    canonical_url = site["canonical_url"]
    assert isinstance(canonical_url, str)
    if parser.canonicals and parser.canonicals != [canonical_url]:
        raise ValueError("canonical metadata does not match profile.json")
    robots = (site_dir / "robots.txt").read_text(encoding="utf-8")
    if noindex:
        if "noindex,nofollow" not in robots_values:
            raise ValueError("noindex build is missing robots metadata")
        if (site_dir / "sitemap.xml").exists():
            raise ValueError("noindex build must not include a sitemap")
        if robots != "User-agent: *\nAllow: /\n":
            raise ValueError("noindex robots.txt must allow crawlers to read metadata")
    else:
        if "index,follow" not in robots_values:
            raise ValueError("indexable build is missing robots metadata")
        if parser.canonicals != [canonical_url]:
            raise ValueError("indexable build canonical metadata does not match profile.json")
        sitemap = site_dir / "sitemap.xml"
        if not sitemap.is_file():
            raise ValueError("indexable build is missing sitemap.xml")
        expected_sitemap_url = urljoin(canonical_url, "sitemap.xml")
        expected_robots = (
            f"User-agent: *\nAllow: /\nSitemap: {expected_sitemap_url}\n"
        )
        if robots != expected_robots:
            raise ValueError("indexable robots.txt does not match the canonical URL")
        try:
            sitemap_root = ElementTree.fromstring(sitemap.read_text(encoding="utf-8"))
        except (ElementTree.ParseError, UnicodeDecodeError) as error:
            raise ValueError("sitemap.xml must contain valid XML") from error
        locations = [
            element.text
            for element in sitemap_root.iter()
            if element.tag.rpartition("}")[2] == "loc"
        ]
        if locations != [canonical_url]:
            raise ValueError("sitemap.xml URL does not match profile.json")


def _validate_css_text(
    site_dir: Path, source: Path, css: str, *, label: str
) -> None:
    normalized = _CSS_COMMENT.sub("", css)
    if "\\" in normalized:
        raise ValueError(f"{label} cannot use CSS escapes")
    construct = _CSS_NON_URL_RESOURCE.search(normalized)
    if construct is not None:
        raise ValueError(
            f"{label} may only load validated local url() assets; "
            f"found {construct.group(0)!r}"
        )
    if _REMOTE_REFERENCE.search(normalized):
        raise ValueError(f"{label} contains a remote reference")
    matches = list(_CSS_URL.finditer(normalized))
    if len(matches) != len(_CSS_URL_START.findall(normalized)):
        raise ValueError(f"{label} contains an unsupported url() reference")
    references = [match.group(2).strip() for match in matches]
    for reference in references:
        try:
            _local_reference(site_dir, source, reference, "CSS asset reference")
        except ValueError as error:
            raise ValueError(f"unsafe CSS asset reference: {reference}") from error


def _validate_css(site_dir: Path, css_path: Path) -> None:
    css = css_path.read_text(encoding="utf-8")
    normalized = _CSS_COMMENT.sub("", css)
    if css_path.name == "custom.css":
        construct = _CSS_ANY_RESOURCE.search(normalized)
        if construct is not None or _REMOTE_REFERENCE.search(normalized):
            raise ValueError(
                "custom CSS cannot load resources; remove @import, url(), "
                "image-set(), -webkit-image-set(), src(), and remote URLs"
            )
        if "\\" in normalized:
            raise ValueError("custom CSS cannot use escaped resource tokens")
        return
    _validate_css_text(site_dir, css_path, css, label="packaged CSS asset")


def _validate_svg(site_dir: Path, svg_path: Path) -> None:
    try:
        root = ElementTree.fromstring(svg_path.read_text(encoding="utf-8"))
    except (ElementTree.ParseError, UnicodeDecodeError) as error:
        raise ValueError(f"SVG asset must contain valid XML: {svg_path}") from error
    for element in root.iter():
        for attribute, reference in element.attrib.items():
            name = attribute.rpartition("}")[2]
            if name in {"href", "src"}:
                try:
                    _local_reference(
                        site_dir, svg_path, reference, "SVG asset reference"
                    )
                except ValueError as error:
                    raise ValueError(
                        f"unsafe SVG asset reference: {reference}"
                    ) from error
            elif name == "style":
                _validate_css_text(
                    site_dir, svg_path, reference, label="SVG CSS asset"
                )
        if element.tag.rpartition("}")[2] == "style" and element.text:
            _validate_css_text(
                site_dir, svg_path, element.text, label="SVG CSS asset"
            )


def _validate_ownership_marker(site_dir: Path, *, required: bool) -> bool:
    marker = site_dir / _OWNERSHIP_MARKER
    try:
        mode = marker.lstat().st_mode
    except FileNotFoundError:
        if required:
            raise ValueError(
                "existing custom profile output is not owned by Tokenmaxxing"
            )
        return False
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise ValueError("Tokenmaxxing ownership marker must be a regular file")
    try:
        payload = _strict_json(
            marker.read_text(encoding="utf-8"), "Tokenmaxxing ownership marker"
        )
    except UnicodeDecodeError as error:
        raise ValueError(
            "Tokenmaxxing ownership marker must contain strict UTF-8 JSON"
        ) from error
    if (
        not isinstance(payload, dict)
        or set(payload) != {"kind", "schema_version"}
        or payload["kind"] != "tokenmaxxing-profile"
        or type(payload["schema_version"]) is not int
        or payload["schema_version"] != 1
    ):
        raise ValueError("Tokenmaxxing ownership marker has an invalid schema")
    return True


def _write_ownership_marker(site_dir: Path) -> None:
    (site_dir / _OWNERSHIP_MARKER).write_text(
        json.dumps(_OWNERSHIP_PAYLOAD, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def validate_site(site_dir: Path, *, noindex: bool) -> None:
    _, files = _site_entries(site_dir)
    _validate_ownership_marker(site_dir, required=False)
    for relative in _REQUIRED_FILES:
        candidate = site_dir / relative
        if not candidate.is_file() or candidate.is_symlink():
            raise ValueError(f"missing required site file: {relative}")
    try:
        payload = json.loads((site_dir / "profile.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError("profile.json must contain valid UTF-8 JSON") from error
    public_payload = _validate_public_payload(payload)
    _validate_html(site_dir, public_payload, noindex=noindex)
    for css_path in (path for path in files if path.suffix.lower() == ".css"):
        _validate_css(site_dir, css_path)
    for svg_path in (path for path in files if path.suffix.lower() == ".svg"):
        _validate_svg(site_dir, svg_path)
    scripts = b"".join(
        path.read_bytes()
        for path in sorted(files)
        if path.suffix.lower() == ".js"
    )
    if len(gzip.compress(scripts)) >= _JAVASCRIPT_BUDGET:
        raise ValueError("compressed JavaScript exceeds the 15 KB budget")
    for script_path in (path for path in files if path.suffix.lower() == ".js"):
        try:
            script_text = script_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise ValueError(f"JavaScript asset must be UTF-8: {script_path}") from error
        if _REMOTE_EXECUTABLE.search(script_text):
            raise ValueError(
                f"JavaScript asset contains a remote executable reference: {script_path}"
            )
        network_token = _NETWORK_JAVASCRIPT.search(script_text)
        if network_token is not None:
            raise ValueError(
                "JavaScript asset contains a network-capable JavaScript token "
                f"{network_token.group(0)!r}: {script_path}"
            )
    documents = b"".join(
        path.read_bytes()
        for path in files
        if path.suffix.lower() in {".html", ".css"}
    )
    if len(gzip.compress(documents)) >= _DOCUMENT_BUDGET:
        raise ValueError("compressed HTML and CSS exceed the 60 KB budget")


def _rename_directory(source: Path, destination: Path) -> None:
    source.rename(destination)


def _remove_directory(path: Path) -> None:
    shutil.rmtree(path)


def _fsync_tree(site_dir: Path) -> None:
    directories, files = _site_entries(site_dir)
    for path in files:
        try:
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
        except OSError:
            pass
    for path in reversed(directories):
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError:
            continue
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)


def _existing_kind(path: Path) -> str | None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "directory"
    return "file"


def _destination_identity(path: Path) -> tuple[int, int] | None:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return None
    return details.st_dev, details.st_ino


def _validate_existing_destination(
    destination: Path, *, managed: bool
) -> tuple[int, int] | None:
    destination_kind = _existing_kind(destination)
    if destination_kind not in {None, "directory"}:
        raise ValueError("profile destination must be a directory, not a symlink or file")
    if destination_kind is None:
        return None
    if not managed:
        _validate_ownership_marker(destination, required=True)
    noindex = not (destination / "sitemap.xml").is_file()
    try:
        validate_site(destination, noindex=noindex)
    except ValueError as error:
        scope = "managed profile site" if managed else "owned custom profile output"
        raise ValueError(f"existing {scope} is not a valid prior site: {error}") from error
    return _destination_identity(destination)


def _replace_directory(
    temporary: Path,
    destination: Path,
    expected_destination: tuple[int, int] | None,
) -> None:
    source_parent = temporary.parent.resolve()
    destination_parent = destination.parent.resolve()
    if source_parent != destination_parent:
        raise ValueError("temporary and destination directories must share a parent")
    if temporary.resolve() == source_parent or destination.resolve(strict=False) == destination_parent:
        raise ValueError("refusing to replace a parent directory")
    if _existing_kind(temporary) != "directory":
        raise ValueError("temporary site must be a real directory")
    destination_kind = _existing_kind(destination)
    if destination_kind not in {None, "directory"}:
        raise ValueError("profile destination must be a directory, not a symlink or file")
    if _destination_identity(destination) != expected_destination:
        raise ValueError("profile destination changed while the site was being built")

    backup: Path | None = None
    if destination_kind == "directory":
        backup = destination.parent / f".{destination.name}-backup-{uuid4().hex}"
        _rename_directory(destination, backup)
    try:
        _rename_directory(temporary, destination)
    except BaseException as replace_error:
        if backup is None:
            raise
        if _existing_kind(destination) is not None:
            raise BuildRecoveryError(
                f"profile replacement failed; previous site retained at {backup}"
            ) from replace_error
        try:
            _rename_directory(backup, destination)
        except BaseException as restore_error:
            raise BuildRecoveryError(
                f"profile replacement and restoration failed; recover the previous site from {backup}"
            ) from restore_error
        raise

    if backup is not None:
        try:
            _remove_directory(backup)
        except OSError:
            warnings.warn(
                f"new profile is live; retained previous site backup at {backup}",
                BuildCleanupWarning,
                stacklevel=2,
            )


def _build_result(destination: Path, generated_at: datetime) -> BuildResult:
    _, files = _site_entries(destination)
    return BuildResult(
        site_dir=destination,
        generated_at=generated_at,
        file_count=len(files),
        total_bytes=sum(path.stat().st_size for path in files),
    )


def build_profile(
    config_path: Path,
    *,
    db_path: Path,
    output: Path | None = None,
    noindex: bool = False,
    now: datetime | None = None,
) -> BuildResult:
    database_path = db_path.expanduser()
    if not database_path.is_file():
        raise FileNotFoundError(f"usage database does not exist: {database_path}")
    config = load_config(config_path)
    paths = profile_paths(config_path)
    destination = output.expanduser().absolute() if output else paths.site
    managed_destination = output is None
    expected_destination = _validate_existing_destination(
        destination, managed=managed_destination
    )

    database = Database.open(database_path)
    try:
        database.connection.execute("BEGIN")
        rows = Repository(database).profile_reporting_rows()
        database.connection.commit()
    except BaseException:
        database.connection.rollback()
        raise
    finally:
        database.close()

    generated_at = now or datetime.now(config.site.timezone)
    data = build_profile_data(
        rows,
        timezone=config.site.timezone,
        now=generated_at,
        window_days=config.metrics.window_days,
    )
    effective_noindex = noindex or not config.site.indexable
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent)
    )
    try:
        render_site(config, data, paths, temporary, noindex=effective_noindex)
        _write_ownership_marker(temporary)
        validate_site(temporary, noindex=effective_noindex)
        _fsync_tree(temporary)
        if (
            _validate_existing_destination(
                destination, managed=managed_destination
            )
            != expected_destination
        ):
            raise ValueError("profile destination changed while the site was being built")
        _replace_directory(temporary, destination, expected_destination)
    except BaseException:
        if _existing_kind(temporary) == "directory":
            shutil.rmtree(temporary, ignore_errors=True)
        raise
    return _build_result(destination, generated_at)
