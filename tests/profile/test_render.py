import gzip
import json
import re
from dataclasses import fields, replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from tokenmaxxing.models import ProfileUsageRow, ReportingRow, Source
from tokenmaxxing.pricing import ApiValueEstimate
from tokenmaxxing.profile.config import (
    DeployConfig,
    MetricsConfig,
    ProfileConfig,
    ProfileInfo,
    ProfileLink,
    ScheduleConfig,
    SiteConfig,
)
from tokenmaxxing.profile.build import validate_site
from tokenmaxxing.profile.data import HarnessTotal, ModelTotal, build_profile_data
from tokenmaxxing.profile.project import profile_paths
from tokenmaxxing.profile.render import render_site


def _config(root: Path, *, avatar: Path | None = None) -> ProfileConfig:
    return ProfileConfig(
        version=1,
        profile=ProfileInfo(
            name="Ada Lovelace",
            role="Programmer",
            bio="Makes machines think.",
            avatar=avatar,
            links=(
                ProfileLink(
                    label="Website",
                    value="ada.example",
                    url="https://ada.example/",
                ),
            ),
        ),
        site=SiteConfig(
            title="Ada's token trail",
            description="Aggregate local AI agent usage.",
            canonical_url="https://example.com/tokens/",
            indexable=True,
            timezone=ZoneInfo("UTC"),
        ),
        metrics=MetricsConfig(),
        deploy=DeployConfig(),
        schedule=ScheduleConfig(),
    )


def _data():
    timezone = ZoneInfo("UTC")
    return build_profile_data(
        (),
        timezone=timezone,
        now=datetime(2026, 8, 30, 12, tzinfo=timezone),
        window_days=28,
    )


def _usage_row(
    *, source: Source, model: str, provider: str | None, tokens: int
) -> ProfileUsageRow:
    return ProfileUsageRow(
        usage=ReportingRow(
            source=source,
            granularity="model_call",
            provider=provider,
            resolved_model=model,
            requested_model=model,
            occurred_at_ns=int(
                datetime(2026, 8, 30, 9, tzinfo=ZoneInfo("UTC")).timestamp()
                * 1_000_000_000
            ),
            input_tokens=tokens,
            output_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
            cache_write_5m_tokens=None,
            cache_write_1h_tokens=None,
            reasoning_tokens=0,
            reported_total_tokens=tokens,
            derived_total_tokens=tokens,
            total_cost_nanos=None,
            service_tier=None,
            speed=None,
            inference_region=None,
        ),
        agent_key=None,
    )


def _paths(root: Path):
    config = root / "tokenmaxxing.yaml"
    config.write_text("version: 1\n", encoding="utf-8")
    return profile_paths(config)


def test_preview_renders_escaped_visible_content_and_fixed_charts(tmp_path: Path) -> None:
    hostile = replace(
        _config(tmp_path),
        profile=replace(
            _config(tmp_path).profile,
            name='<script>alert("private")</script>',
        ),
    )
    destination = tmp_path / "site"

    render_site(hostile, _data(), _paths(tmp_path), destination, noindex=True)
    html = (destination / "index.html").read_text(encoding="utf-8")

    assert '<script>alert("private")</script>' not in html
    assert "&lt;script&gt;alert" in html
    assert '<meta name="robots" content="noindex, nofollow">' in html
    assert re.search(
        r'<strong>0</strong>\s*<span class="usage-label">Tokens'
        r"<small>in last 28 days</small></span>",
        html,
    )
    assert html.count('class="activity-cell') == 364
    assert html.count('class="agent-bar') == 28
    assert 'role="tablist"' in html
    assert 'role="tab"' in html
    assert 'data-roving="activity"' in html
    assert 'class="activity-cell state-untracked" aria-hidden="true"' in html
    assert 'class="activity-cell state-untracked" type="button"' not in html
    assert '<script src="assets/profile.js" defer></script>' in html
    assert not (destination / "sitemap.xml").exists()
    assert (destination / "robots.txt").read_text(encoding="utf-8") == "User-agent: *\nDisallow: /\n"


def test_real_render_passes_site_validation(tmp_path: Path) -> None:
    destination = tmp_path / "site"

    render_site(_config(tmp_path), _data(), _paths(tmp_path), destination, noindex=True)

    validate_site(destination, noindex=True)


def test_first_tracked_activity_day_owns_the_roving_tab_stop(tmp_path: Path) -> None:
    base = _data()
    first_tracked = base.activity_days[-2].day
    data = replace(base, first_tracked_day=first_tracked)
    destination = tmp_path / "site"

    render_site(_config(tmp_path), data, _paths(tmp_path), destination, noindex=True)
    html = (destination / "index.html").read_text(encoding="utf-8")

    assert html.count('class="activity-cell state-untracked" aria-hidden="true"') == 362
    tracked = re.findall(r'<button class="activity-cell state-quiet"[^>]+>', html)
    assert len(tracked) == 2
    assert 'tabindex="0"' in tracked[0]
    assert 'tabindex="-1"' in tracked[1]
    assert all('aria-describedby="activity-tooltip"' in cell for cell in tracked)


def test_indexable_render_has_metadata_and_only_local_assets(tmp_path: Path) -> None:
    avatar = tmp_path / "avatar.webp"
    avatar.write_bytes(b"avatar")
    (tmp_path / "custom.css").write_text(".profile-shell { letter-spacing: 0; }\n")
    destination = tmp_path / "site"

    rendered = render_site(
        _config(tmp_path, avatar=avatar),
        _data(),
        _paths(tmp_path),
        destination,
        noindex=False,
    )
    html = (destination / "index.html").read_text(encoding="utf-8")

    assert '<link rel="canonical" href="https://example.com/tokens/">' in html
    assert '<meta property="og:type" content="profile">' in html
    assert '<meta name="twitter:card" content="summary">' in html
    assert '"@type":"ProfilePage"' in html
    assert (destination / "sitemap.xml").exists()
    assert (destination / "assets" / "avatar.webp").read_bytes() == b"avatar"
    assert (destination / "assets" / "custom.css").exists()
    assert (destination / "profile.json") in rendered.files
    assert not re.findall(r'<(?:script|img)[^>]+src="https?://', html)
    assert not re.findall(r'<link[^>]+rel="stylesheet"[^>]+href="https?://', html)
    assert "fonts/newsreader-latin.woff2" in (destination / "assets" / "profile.css").read_text()


def test_missing_configured_avatar_fails_before_render(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="avatar"):
        render_site(
            _config(tmp_path, avatar=tmp_path / "missing.webp"),
            _data(),
            _paths(tmp_path),
            tmp_path / "site",
            noindex=True,
        )


def test_model_and_harness_panels_render_empty_and_unknown_names_cleanly(tmp_path: Path) -> None:
    data = replace(
        _data(),
        models=(ModelTotal(model="Unknown model", total_tokens=4),),
        harnesses=(HarnessTotal(harness="opencode", total_tokens=4),),
    )
    destination = tmp_path / "site"

    render_site(_config(tmp_path), data, _paths(tmp_path), destination, noindex=True)
    html = (destination / "index.html").read_text(encoding="utf-8")

    assert "unknown models" in html
    assert "Unknown model" not in html
    assert 'data-panel="models"' in html
    assert 'data-panel="harnesses"' in html
    assert "icon-opencode" in html
    assert "icon-generic" in html


def test_real_model_rows_select_provider_icons_without_name_guessing(tmp_path: Path) -> None:
    timezone = ZoneInfo("UTC")
    data = build_profile_data(
        (
            _usage_row(
                source="claude",
                model="mystery-one",
                provider="anthropic",
                tokens=30,
            ),
            _usage_row(
                source="opencode",
                model="mystery-two",
                provider=None,
                tokens=20,
            ),
            _usage_row(
                source="codex",
                model="claude-looking-name",
                provider=None,
                tokens=10,
            ),
        ),
        timezone=timezone,
        now=datetime(2026, 8, 30, 12, tzinfo=timezone),
        window_days=28,
    )
    destination = tmp_path / "site"

    render_site(_config(tmp_path), data, _paths(tmp_path), destination, noindex=True)
    html = (destination / "index.html").read_text(encoding="utf-8")
    payload = json.loads((destination / "profile.json").read_text(encoding="utf-8"))
    icons = {model["name"]: model["icon"] for model in payload["stats"]["models"]}

    assert "mystery-one" in html and "icon-claude" in html
    assert "mystery-two" in html and "icon-opencode" in html
    assert "claude-looking-name" in html
    assert html.count("icon-generic") == 1
    assert icons == {
        "mystery-one": "claude",
        "mystery-two": "opencode",
        "claude-looking-name": "generic",
    }


def test_assets_are_licensed_local_and_small(tmp_path: Path) -> None:
    destination = tmp_path / "site"
    render_site(_config(tmp_path), _data(), _paths(tmp_path), destination, noindex=True)

    script = (destination / "assets" / "profile.js").read_bytes()
    assert len(gzip.compress(script)) < 15_000
    assert b"fetch(" not in script
    assert (destination / "assets" / "ASSET_SOURCES.md").exists()
    assert (destination / "assets" / "licenses" / "Lobe-Icons-MIT.txt").exists()
    assert (destination / "assets" / "fonts" / "OFL.txt").exists()
    assert (destination / "assets" / "icons" / "awards.svg").exists()
    assert all(
        (destination / "assets" / "icons" / f"{name}.svg").exists()
        for name in (
            "openai",
            "claude",
            "opencode",
            "google",
            "deepseek",
            "zai",
            "moonshot",
            "xai",
            "mistral",
            "qwen",
        )
    )
    css = (destination / "assets" / "profile.css").read_text(encoding="utf-8")
    assert "mask-image: var(--icon)" in css
    assert 'url("icons/openai.svg")' in css
    assert '<img class="model-icon' not in (destination / "index.html").read_text(
        encoding="utf-8"
    )


def test_awards_render_a_face_metric_and_optional_earned_date(tmp_path: Path) -> None:
    base = _data()
    data = SimpleNamespace(
        **{
            field.name: getattr(base, field.name)
            for field in fields(base)
            if field.name != "awards"
        },
        awards=(
            SimpleNamespace(
                key="billion-day",
                name="Billion Day",
                description="Tracked one billion tokens in a day.",
                metric_value=1_500_000_000,
                earned_on=datetime(2026, 8, 21, tzinfo=ZoneInfo("UTC")).date(),
            ),
        ),
    )
    destination = tmp_path / "site"

    render_site(_config(tmp_path), data, _paths(tmp_path), destination, noindex=True)
    html = (destination / "index.html").read_text(encoding="utf-8")

    assert 'aria-labelledby="awards-title"' in html
    assert '>Awards<' in html
    assert '>1B<' not in html
    assert 'class="award-medal award-billion-day"' in html
    assert 'class="award-art"' in html
    assert '<span class="award-name">Billion Day</span>' in html
    assert '<span class="award-summary">1.5B tokens</span>' in html
    assert 'aria-hidden="true" focusable="false"' in html
    assert 'href="assets/icons/awards.svg#award-billion-day"' in html
    assert (
        'aria-label="Billion Day. Tracked one billion tokens in a day. '
        '1.5B tokens. Earned Aug 21, 2026."' in html
    )
    assert "Billion Day" in html
    assert "1.5B tokens" in html
    assert "Aug 21, 2026" in html
    payload = json.loads((destination / "profile.json").read_text(encoding="utf-8"))
    assert set(payload["stats"]["awards"][0]) == {
        "key",
        "name",
        "description",
        "metric_value",
        "earned_on",
    }


def test_award_tooltips_are_opaque_and_stack_above_sibling_awards(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "site"
    render_site(_config(tmp_path), _data(), _paths(tmp_path), destination, noindex=True)

    css = (destination / "assets" / "profile.css").read_text(encoding="utf-8")

    assert re.search(
        r"\.award-medal:hover\s*,\s*\.award-medal:focus-visible\s*,\s*"
        r'\.award-medal\[aria-expanded="true"\]\s*\{[^}]*z-index:\s*20',
        css,
    )
    assert re.search(r"\.profile-rail\s*\{[^}]*z-index:\s*10", css, re.DOTALL)
    tooltip_rule = re.search(r"\.award-tooltip\s*\{([^}]*)\}", css, re.DOTALL)
    assert tooltip_rule is not None
    award_tooltip = tooltip_rule.group(1)
    assert "border: 1px solid var(--line)" in award_tooltip
    assert "background: var(--paper)" in award_tooltip
    assert "color: var(--ink)" in award_tooltip
    assert "box-shadow: 0 6px 20px rgba(37, 32, 27, 0.15)" in award_tooltip
    assert "--tooltip-surface:" not in css
    sprite = (destination / "assets" / "icons" / "awards.svg").read_text(
        encoding="utf-8"
    )
    hot_streak = re.search(
        r'<symbol id="award-hot-streak"[^>]*>(.*?)</symbol>', sprite, re.DOTALL
    )
    assert hot_streak is not None
    assert hot_streak.group(1).count('class="award-flame"') == 1
    assert re.search(
        r"\.award-medal:hover \.award-tooltip\s*,\s*"
        r"\.award-medal:focus-visible \.award-tooltip\s*,\s*"
        r'\.award-medal\[aria-expanded="true"\] \.award-tooltip\s*'
        r"\{[^}]*visibility:\s*visible",
        css,
    )
    assert "transition: opacity" not in award_tooltip


def test_all_tooltip_triggers_expose_tap_state(tmp_path: Path) -> None:
    destination = tmp_path / "site"
    base = _data()
    data = replace(
        base,
        first_tracked_day=base.activity_days[-2].day,
        awards=(
            SimpleNamespace(
                key="hot-streak",
                name="Hot Streak",
                description="Stayed active for fourteen days.",
                metric_value=14,
                earned_on=None,
            ),
        ),
    )
    render_site(_config(tmp_path), data, _paths(tmp_path), destination, noindex=True)

    html = (destination / "index.html").read_text(encoding="utf-8")
    css = (destination / "assets" / "profile.css").read_text(encoding="utf-8")
    script = (destination / "assets" / "profile.js").read_text(encoding="utf-8")

    assert re.search(r'class="activity-cell[^\"]*"[^>]*aria-expanded="false"', html)
    assert re.search(r'class="agent-bar"[^>]*aria-expanded="false"', html)
    assert re.search(r'class="award-medal[^\"]*"[^>]*aria-expanded="false"', html)
    assert '[aria-expanded="true"] .award-tooltip' in css
    assert "activeTapTooltip" in script
    assert 'event.key === "Escape"' in script
    assert 'document.addEventListener("click"' in script


def test_window_label_uses_the_profile_data_window(tmp_path: Path) -> None:
    data = replace(
        _data(),
        window_start=datetime(2026, 8, 24, tzinfo=ZoneInfo("UTC")).date(),
        window_end=datetime(2026, 8, 30, tzinfo=ZoneInfo("UTC")).date(),
    )
    destination = tmp_path / "site"

    render_site(_config(tmp_path), data, _paths(tmp_path), destination, noindex=True)

    html = (destination / "index.html").read_text(encoding="utf-8")

    assert "in last 7 days" in html
    assert re.search(
        r'<span class="usage-label">Tokens<small>in last 7 days</small>',
        html,
    )
    assert "Last 7 Days ·" not in html
    assert "all time" not in html


def test_profile_controls_and_awards_adapt_without_mobile_orphans(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "site"
    render_site(_config(tmp_path), _data(), _paths(tmp_path), destination, noindex=True)

    css = (destination / "assets" / "profile.css").read_text(encoding="utf-8")

    html = (destination / "index.html").read_text(encoding="utf-8")
    sprite = (destination / "assets" / "icons" / "awards.svg").read_text(
        encoding="utf-8"
    )

    assert 'class="theme-icon theme-icon-sun"' in html
    assert 'class="theme-icon theme-icon-moon"' in html
    assert ">Theme</button>" not in html
    profile_links = re.search(r"\.profile-links\s*\{([^}]*)\}", css, re.DOTALL)
    assert profile_links is not None
    assert "border-top:" not in profile_links.group(1)
    assert "padding: 17px 0 0" not in profile_links.group(1)
    assert "grid-template-columns: 44px minmax(0, 1fr)" in css
    assert "min-height: 44px" in css
    assert "@media (max-width: 760px)" in css
    assert re.search(
        r"@media \(max-width: 760px\)[\s\S]*?\.theme-dock\s*\{[^}]*position:\s*absolute[^}]*top:\s*18px[^}]*right:",
        css,
    )
    assert re.search(
        r"@media \(max-width: 760px\)[\s\S]*?\.profile-links\s*\{[^}]*grid-template-columns:\s*repeat\(3,",
        css,
    )
    assert re.search(
        r"@media \(max-width: 340px\)[\s\S]*?\.profile-links\s*\{[^}]*grid-template-columns:\s*1fr",
        css,
    )
    assert re.search(r"\.wordmark\s*\{[^}]*margin:\s*0 0 56px", css)
    assert re.search(r"\.usage\s*\{[^}]*padding:\s*60px 0 30px", css)
    assert sprite.count('class="award-coin"') == 7
    assert "award-ribbon" not in sprite
    assert "@media (prefers-reduced-motion: no-preference)" in css
    assert "animation-delay: calc(var(--award-index) * 30ms)" in css
    assert "@media (prefers-reduced-motion: reduce)" in css


def test_api_equivalent_is_subtly_highlighted_when_present(tmp_path: Path) -> None:
    destination = tmp_path / "site"
    data = replace(
        _data(),
        api_equivalent=ApiValueEstimate(
            cost_nanos=8_100_000_000_000,
            priced_tokens=1_000,
            total_tokens=1_000,
            priced_events=1,
            total_events=1,
            by_provider=(),
        ),
    )
    render_site(_config(tmp_path), data, _paths(tmp_path), destination, noindex=True)

    html = (destination / "index.html").read_text(encoding="utf-8")
    css = (destination / "assets" / "profile.css").read_text(encoding="utf-8")

    assert 'class="status-highlight"' in html
    assert re.search(
        r"\.status-highlight dd\s*\{[^}]*color:\s*var\(--accent\)[^}]*"
        r"font-weight:\s*680",
        css,
    )


def test_project_template_override_stays_inside_generated_output(tmp_path: Path) -> None:
    template = tmp_path / "template"
    template.mkdir()
    (template / "index.html.j2").write_text(
        "<!doctype html><title>{{ site.title }}</title><p>{{ profile.name }}</p>",
        encoding="utf-8",
    )
    destination = tmp_path / "site"

    render_site(_config(tmp_path), _data(), _paths(tmp_path), destination, noindex=True)

    html = (destination / "index.html").read_text(encoding="utf-8")
    payload = json.loads((destination / "profile.json").read_text(encoding="utf-8"))
    assert html == "<!doctype html><title>Ada&#39;s token trail</title><p>Ada Lovelace</p>"
    assert str(tmp_path) not in json.dumps(payload)
