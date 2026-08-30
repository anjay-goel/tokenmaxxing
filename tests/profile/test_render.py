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
    config = root / "config.yaml"
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
    assert re.search(r'<script src="assets/profile\.js\?v=\d+" defer>', html)
    assert re.search(r'<link rel="stylesheet" href="assets/profile\.css\?v=\d+">', html)
    assert re.search(
        r'<strong>0</strong>\s*<span class="usage-label">Tokens in last 28 days</span>',
        html,
    )
    assert html.count('class="activity-cell') == 364
    assert '<h2 id="activity-title">Token Trail</h2>' in html
    assert '<h2 id="trend-title">Activity</h2>' in html
    assert html.count('class="trend-hit"') == 28
    assert 'class="trend-area"' in html
    assert 'class="trend-line"' in html
    assert "trend-scope" not in html
    assert html.index('id="activity-title"') < html.index('id="trend-title"')
    assert html.index('id="trend-title"') < html.index('id="agents-title"')
    assert html.count('class="agent-bar') == 28
    assert 'role="tablist"' in html
    assert 'role="tab"' in html
    assert 'data-roving="activity"' in html
    assert 'class="activity-cell state-untracked" aria-hidden="true"' in html
    assert 'class="activity-cell state-untracked" type="button"' not in html
    assert (
        '<footer class="site-attribution">Generated with '
        '<a href="https://github.com/anjay-goel/tokenmaxxing" '
        'rel="noopener noreferrer">tokenmaxxing</a></footer>' in html
    )
    assert not (destination / "sitemap.xml").exists()
    assert (destination / "robots.txt").read_text(encoding="utf-8") == "User-agent: *\nAllow: /\n"


def test_real_render_passes_site_validation(tmp_path: Path) -> None:
    destination = tmp_path / "site"

    render_site(_config(tmp_path), _data(), _paths(tmp_path), destination, noindex=True)

    validate_site(destination, noindex=True)


def test_profile_links_use_recognizable_brand_and_website_icons(tmp_path: Path) -> None:
    config = replace(
        _config(tmp_path),
        profile=replace(
            _config(tmp_path).profile,
            links=(
                ProfileLink("LinkedIn", "ada", "https://linkedin.com/in/ada"),
                ProfileLink("GitHub", "ada", "https://github.com/ada"),
                ProfileLink("Website", "ada.example", "https://ada.example"),
            ),
        ),
    )
    destination = tmp_path / "site"

    render_site(config, _data(), _paths(tmp_path), destination, noindex=True)

    html = (destination / "index.html").read_text(encoding="utf-8")
    assert 'class="profile-link-icon icon-linkedin"' in html
    assert 'class="profile-link-icon icon-github"' in html
    assert 'class="profile-link-icon icon-website"' in html


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
        models=(ModelTotal(model="(unknown)", total_tokens=4),),
        harnesses=(
            HarnessTotal(harness="opencode", total_tokens=4),
            HarnessTotal(harness="pi", total_tokens=3),
        ),
    )
    destination = tmp_path / "site"

    render_site(_config(tmp_path), data, _paths(tmp_path), destination, noindex=True)
    html = (destination / "index.html").read_text(encoding="utf-8")

    assert "unknown models" in html
    assert "(unknown)" not in html
    assert 'data-panel="models"' in html
    assert 'data-panel="harnesses"' in html
    assert "icon-opencode" in html
    assert "icon-pi" in html
    assert "icon-generic" in html


def test_real_model_rows_prefer_creator_metadata_then_model_family(tmp_path: Path) -> None:
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
                source="pi",
                model="gpt-5.4-mini",
                provider="openai-codex",
                tokens=20,
            ),
            _usage_row(
                source="opencode",
                model="llama-3.3-70b",
                provider="openrouter",
                tokens=10,
            ),
            _usage_row(source="opencode", model="mystery-two", provider=None, tokens=5),
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
    assert "gpt-5.4-mini" in html and "icon-openai" in html
    assert "llama-3.3-70b" in html and "icon-meta" in html
    assert "mystery-two" in html
    assert icons == {
        "mystery-one": "claude",
        "gpt-5.4-mini": "openai",
        "llama-3.3-70b": "meta",
        "mystery-two": "generic",
    }


def test_popular_model_families_have_creator_icons(tmp_path: Path) -> None:
    expected = {
        "gpt-5.4": "openai",
        "claude-opus-5": "claude",
        "gemini-3-pro": "google",
        "deepseek-r1": "deepseek",
        "glm-4.7": "zai",
        "kimi-k2": "kimi",
        "grok-4": "xai",
        "mistral-large": "mistral",
        "qwen3-coder": "qwen",
        "llama-4-maverick": "meta",
        "command-r-plus": "cohere",
        "minimax-m2": "minimax",
        "baichuan4": "baichuan",
        "yi-lightning": "yi",
        "hunyuan-t1": "hunyuan",
        "doubao-seed": "doubao",
        "internlm3": "internlm",
        "phi-4": "microsoft",
        "ernie-4.5": "wenxin",
        "nemotron-ultra": "nvidia",
        "step-3.5-flash": "stepfun",
        "granite-4": "ibm",
        "olmo-2": "ai2",
        "falcon-h1": "tii",
        "rwkv-7": "rwkv",
        "dbrx-instruct": "dbrx",
        "smollm3": "huggingface",
        "sensenova-5.5": "sensenova",
        "skywork-o1": "skywork",
        "mimo-v2-flash": "xiaomimimo",
        "jamba-1.5-large": "ai21",
        "chatglm3-6b": "chatglm",
        "gemma-3-27b": "gemma",
        "longcat-flash": "longcat",
        "zhinao-360": "ai360",
        "sparkdesk-v4": "spark",
        "amazon-nova-pro": "bedrock",
        "sonar-pro": "perplexity",
        "lfm2-24b": "liquid",
        "snowflake-arctic": "snowflake",
    }
    data = replace(
        _data(),
        models=tuple(
            ModelTotal(model=model, total_tokens=index + 1)
            for index, model in enumerate(expected)
        ),
    )
    destination = tmp_path / "site"

    render_site(_config(tmp_path), data, _paths(tmp_path), destination, noindex=True)

    payload = json.loads((destination / "profile.json").read_text(encoding="utf-8"))
    icons = {model["name"]: model["icon"] for model in payload["stats"]["models"]}
    assert icons == expected


def test_creator_metadata_wins_and_host_providers_fall_back_to_model_family(
    tmp_path: Path,
) -> None:
    data = replace(
        _data(),
        models=(
            ModelTotal(model="opaque-openai", total_tokens=5, provider="openai-codex"),
            ModelTotal(model="gpt-5.4", total_tokens=4, provider="anthropic"),
            ModelTotal(model="llama-4-scout", total_tokens=3, provider="openrouter"),
            ModelTotal(model="qwen3", total_tokens=2, provider="ollama"),
            ModelTotal(model="opaque-hosted", total_tokens=1, provider="openrouter"),
        ),
    )
    destination = tmp_path / "site"

    render_site(_config(tmp_path), data, _paths(tmp_path), destination, noindex=True)

    payload = json.loads((destination / "profile.json").read_text(encoding="utf-8"))
    icons = {model["name"]: model["icon"] for model in payload["stats"]["models"]}
    assert icons == {
        "opaque-openai": "openai",
        "gpt-5.4": "claude",
        "llama-4-scout": "meta",
        "qwen3": "qwen",
        "opaque-hosted": "generic",
    }


def test_ambiguous_short_model_fragments_remain_generic(tmp_path: Path) -> None:
    names = ("yifan", "sophia", "stepwise", "command-center", "spark-notebook")
    data = replace(
        _data(),
        models=tuple(ModelTotal(model=name, total_tokens=1) for name in names),
    )
    destination = tmp_path / "site"

    render_site(_config(tmp_path), data, _paths(tmp_path), destination, noindex=True)

    payload = json.loads((destination / "profile.json").read_text(encoding="utf-8"))
    assert {model["name"]: model["icon"] for model in payload["stats"]["models"]} == {
        name: "generic" for name in names
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
            "pi",
            "google",
            "deepseek",
            "zai",
            "moonshot",
            "kimi",
            "xai",
            "mistral",
            "qwen",
            "meta",
            "cohere",
            "minimax",
            "baichuan",
            "yi",
            "hunyuan",
            "doubao",
            "internlm",
            "microsoft",
            "baidu",
            "wenxin",
            "nvidia",
            "stepfun",
            "ibm",
            "ai2",
            "tii",
            "rwkv",
            "dbrx",
            "huggingface",
            "sensenova",
            "skywork",
            "xiaomimimo",
            "ai21",
            "chatglm",
            "gemma",
            "longcat",
            "ai360",
            "spark",
            "bedrock",
            "perplexity",
            "liquid",
            "snowflake",
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


def test_chart_tooltips_use_safe_structured_content(tmp_path: Path) -> None:
    destination = tmp_path / "site"
    render_site(_config(tmp_path), _data(), _paths(tmp_path), destination, noindex=True)

    css = (destination / "assets" / "profile.css").read_text(encoding="utf-8")
    script = (destination / "assets" / "profile.js").read_text(encoding="utf-8")

    assert "tooltip.replaceChildren()" in script
    assert 'document.createElement("strong")' in script
    assert 'document.createElement("dl")' in script
    assert "toLocaleDateString" in script
    assert re.search(
        r'event\.key === "Escape"[\s\S]*?closeTapTooltip\(\);\s*hideTooltip\(\);',
        script,
    )
    assert "window.innerHeight - box.height - 8" in script
    assert "innerHTML" not in script
    assert ".chart-tooltip-title" in css
    assert ".chart-tooltip-total" in css
    assert ".chart-tooltip-breakdown" in css
    assert re.search(
        r"\.chart-tooltip-breakdown\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\) auto",
        css,
        re.DOTALL,
    )


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
        r'<span class="usage-label">Tokens in last 7 days</span>',
        html,
    )
    assert '<h2 id="trend-title">Activity</h2>' in html
    assert html.count('class="trend-hit"') == 7
    assert "Last 7 Days ·" not in html
    assert "all time" not in html


def test_usage_charts_show_muted_window_edges_and_highlight_tracked_total(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "site"
    render_site(_config(tmp_path), _data(), _paths(tmp_path), destination, noindex=True)

    html = (destination / "index.html").read_text(encoding="utf-8")
    css = (destination / "assets" / "profile.css").read_text(encoding="utf-8")

    assert '<strong class="tracked-total">0</strong> tracked tokens' in html
    assert html.count('<div class="chart-range" aria-hidden="true">') == 2
    assert html.count("<span>Aug 3</span><span>Aug 30</span>") == 2
    assert 'style="--agent-days: 28"' in html
    assert (
        '<svg class="trend-chart" viewBox="0 0 900 136" '
        'preserveAspectRatio="none" aria-hidden="true">' in html
    )
    assert re.search(r"\.tracked-total\s*\{[^}]*color:\s*var\(--accent\)", css, re.DOTALL)
    assert re.search(r"\.chart-range\s*\{[^}]*color:\s*var\(--muted\)", css, re.DOTALL)


def test_usage_headline_keeps_unified_label_near_the_amount_baseline(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "site"
    render_site(_config(tmp_path), _data(), _paths(tmp_path), destination, noindex=True)

    css = (destination / "assets" / "profile.css").read_text(encoding="utf-8")

    headline = re.search(r"\.usage h1\s*\{([^}]*)\}", css, re.DOTALL)
    amount = re.search(r"\.usage h1 strong\s*\{([^}]*)\}", css, re.DOTALL)
    label = re.search(r"\.usage-label\s*\{([^}]*)\}", css, re.DOTALL)
    assert headline is not None
    assert amount is not None
    assert label is not None
    assert "align-items: baseline" in headline.group(1)
    assert "flex-wrap: nowrap" in headline.group(1)
    assert "white-space: nowrap" in headline.group(1)
    assert "clamp(62px, 10vw, 106px)" in amount.group(1)
    assert "display: inline-block" in label.group(1)
    assert "clamp(20px, 3.4vw, 36px)" in label.group(1)
    assert "transform: translateY(-4px)" in label.group(1)
    assert ".usage-period" not in css
    assert ".usage-label { font-size: 21px; }" in css
    assert ".usage-label { font-size: 17px; }" in css


def test_agents_chart_uses_distinct_model_colors_in_both_themes(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "site"
    render_site(_config(tmp_path), _data(), _paths(tmp_path), destination, noindex=True)

    css = (destination / "assets" / "profile.css").read_text(encoding="utf-8")

    for color in (
        "#98431f",
        "#32688a",
        "#59752c",
        "#7b4c7c",
        "#b07824",
        "#4f746e",
        "#6b647e",
    ):
        assert color in css
    for color in (
        "#d48a52",
        "#74add0",
        "#9fbe66",
        "#c28ac0",
        "#d9b45d",
        "#70aaa0",
        "#9b91bd",
    ):
        assert color in css
    agent_segment = re.search(r"\.agent-bar span\s*\{([^}]*)\}", css, re.DOTALL)
    assert agent_segment is not None
    assert "filter: saturate(1.2) brightness(0.82)" in agent_segment.group(1)


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
    assert "display: flex" in profile_links.group(1)
    assert "flex-direction: column" in profile_links.group(1)
    assert "align-items: flex-start" in profile_links.group(1)
    assert "grid-template-columns" not in profile_links.group(1)
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
        r"\.theme-dock\s*\{[^}]*position:\s*absolute[^}]*top:\s*22px[^}]*right:",
        css,
    )
    assert ".activity-cell.state-untracked { background: var(--untracked); }" in css
    assert ".activity-cell.state-untracked { opacity: 0; }" not in css
    assert "grid-template-columns: repeat(3, minmax(0, max-content))" not in css
    assert re.search(r"\.wordmark\s*\{[^}]*margin:\s*0 0 54px", css)
    assert re.search(
        r"@media \(max-width: 760px\)[\s\S]*?\.wordmark\s*\{[^}]*margin-bottom:\s*38px",
        css,
    )
    assert re.search(
        r"@media \(max-width: 760px\)[\s\S]*?\.profile-rail\s*\{[^}]*padding:\s*7px 0 22px",
        css,
    )
    assert re.search(r"\.usage\s*\{[^}]*padding:\s*82px 0 30px", css)
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
