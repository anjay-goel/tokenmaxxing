import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tokenmaxxing.db import Database
from tokenmaxxing.profile.build import (
    BuildCleanupWarning,
    BuildRecoveryError,
    build_profile,
    validate_site,
)
from tokenmaxxing.profile.project import ProfilePaths, profile_paths


def prepared_project(tmp_path: Path, minimal_config: str) -> ProfilePaths:
    config = tmp_path / "tokenmaxxing.yaml"
    config.write_text(minimal_config, encoding="utf-8")
    (tmp_path / "avatar.webp").write_bytes(b"avatar")
    database = Database.open(tmp_path / "usage.sqlite3")
    database.close()
    return profile_paths(config)


def public_payload(*, awards: bool = True) -> dict[str, object]:
    stats: dict[str, object] = {
        "window_start": "2026-08-03",
        "window_end": "2026-08-30",
        "window_days": 28,
        "total_tokens": 0,
        "all_time_tokens": 0,
        "quip": "The tokens are resting.",
        "cards": [{"label": "Agents", "value": "0"}],
        "activity": [],
        "agents": [],
        "models": [],
        "harnesses": [],
    }
    if awards:
        stats["awards"] = [
            {
                "key": "billion-day",
                "name": "Billion Day",
                "description": "Tracked one billion tokens in a day.",
                "metric_value": 1_500_000_000,
                "earned_on": "2026-08-21",
            }
        ]
    return {
        "schema_version": 1,
        "generated_at": "2026-08-30T12:00:00+00:00",
        "profile": {
            "name": "Ada Lovelace",
            "role": "Programmer",
            "bio": "Makes machines think.",
            "avatar": "assets/avatar.webp",
            "links": [
                {
                    "label": "Website",
                    "value": "ada.example",
                    "url": "https://ada.example/",
                }
            ],
        },
        "site": {
            "title": "Ada's token trail",
            "description": "Aggregate local AI agent usage.",
            "canonical_url": "https://example.com/tokens/",
            "theme": "auto",
            "accent": "violet",
        },
        "stats": stats,
    }


def write_valid_site(destination: Path, *, noindex: bool = True) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    assets = destination / "assets"
    assets.mkdir()
    (assets / "profile.css").write_text("body { color: black; }\n", encoding="utf-8")
    (assets / "profile.js").write_text("document.documentElement.dataset.ready = '1';\n", encoding="utf-8")
    (assets / "avatar.webp").write_bytes(b"avatar")
    metadata = (
        '<meta name="robots" content="noindex, nofollow">'
        if noindex
        else '<meta name="robots" content="index, follow">'
        '<link rel="canonical" href="https://example.com/tokens/">'
    )
    (destination / "index.html").write_text(
        "<!doctype html><html><head>"
        + metadata
        + '<link rel="stylesheet" href="assets/profile.css">'
        '<script src="assets/profile.js"></script></head>'
        '<body data-total-tokens="0" data-all-time-tokens="0">'
        '<a href="https://ada.example/">ada.example</a>'
        '<img src="assets/avatar.webp" alt="Ada Lovelace">0 Tokens</body></html>',
        encoding="utf-8",
    )
    (destination / "profile.json").write_text(
        json.dumps(public_payload()) + "\n", encoding="utf-8"
    )
    if noindex:
        (destination / "robots.txt").write_text(
            "User-agent: *\nDisallow: /\n", encoding="utf-8"
        )
    else:
        (destination / "robots.txt").write_text(
            "User-agent: *\nAllow: /\nSitemap: https://example.com/tokens/sitemap.xml\n",
            encoding="utf-8",
        )
        (destination / "sitemap.xml").write_text(
            '<?xml version="1.0"?><urlset><url><loc>https://example.com/tokens/</loc></url></urlset>\n',
            encoding="utf-8",
        )


def write_previous_site(destination: Path) -> str:
    write_valid_site(destination, noindex=False)
    index = destination / "index.html"
    previous = index.read_text(encoding="utf-8").replace(
        "0 Tokens</body>", "previous 0 Tokens</body>"
    )
    index.write_text(previous, encoding="utf-8")
    return previous


def test_real_indexable_build_accepts_json_ld_metadata(
    tmp_path: Path, minimal_config: str
) -> None:
    paths = prepared_project(tmp_path, minimal_config)

    result = build_profile(
        paths.config,
        db_path=tmp_path / "usage.sqlite3",
        now=datetime(2026, 8, 30, 12, tzinfo=ZoneInfo("UTC")),
    )

    validate_site(result.site_dir, noindex=False)
    html = (result.site_dir / "index.html").read_text(encoding="utf-8")
    assert 'type="application/ld+json"' in html
    assert "https://schema.org" in html


def test_json_ld_must_be_strict_privacy_safe_json(tmp_path: Path) -> None:
    write_valid_site(tmp_path)
    index = tmp_path / "index.html"
    original = index.read_text(encoding="utf-8")
    index.write_text(
        original.replace(
            "</head>",
            '<script type="application/ld+json">'
            '{"@context":"https://schema.org","session_id":"private"}'
            "</script></head>",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="forbidden public field"):
        validate_site(tmp_path, noindex=True)


def test_json_ld_rejects_invalid_json(tmp_path: Path) -> None:
    write_valid_site(tmp_path)
    index = tmp_path / "index.html"
    original = index.read_text(encoding="utf-8")
    index.write_text(
        original.replace(
            "</head>",
            '<script type="application/ld+json">{"@context":}</script></head>',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="JSON-LD must contain strict JSON"):
        validate_site(tmp_path, noindex=True)


def test_json_ld_rejects_duplicate_keys(tmp_path: Path) -> None:
    write_valid_site(tmp_path)
    index = tmp_path / "index.html"
    original = index.read_text(encoding="utf-8")
    index.write_text(
        original.replace(
            "</head>",
            '<script type="application/ld+json">'
            '{"@context":"https://schema.org","@context":"https://example.com"}'
            "</script></head>",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate keys"):
        validate_site(tmp_path, noindex=True)


def test_custom_output_refuses_an_existing_unowned_directory(
    tmp_path: Path, minimal_config: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    paths = prepared_project(project, minimal_config)
    public_html = tmp_path / "public_html"
    public_html.mkdir()
    sentinel = public_html / "do-not-delete.txt"
    sentinel.write_text("keep me", encoding="utf-8")
    monkeypatch.setattr(
        "tokenmaxxing.profile.build.render_site",
        lambda *args, **kwargs: pytest.fail("renderer must not run"),
    )

    with pytest.raises(ValueError, match="not owned by Tokenmaxxing"):
        build_profile(
            paths.config,
            db_path=project / "usage.sqlite3",
            output=public_html,
        )

    assert sentinel.read_text(encoding="utf-8") == "keep me"


def test_custom_output_refuses_project_root_dot(
    tmp_path: Path, minimal_config: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = prepared_project(tmp_path, minimal_config)
    original_config = paths.config.read_bytes()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "tokenmaxxing.profile.build.render_site",
        lambda *args, **kwargs: pytest.fail("renderer must not run"),
    )

    with pytest.raises(ValueError, match="not owned by Tokenmaxxing"):
        build_profile(
            paths.config,
            db_path=tmp_path / "usage.sqlite3",
            output=Path("."),
        )

    assert paths.config.read_bytes() == original_config
    assert (tmp_path / "usage.sqlite3").is_file()


@pytest.mark.parametrize("kind", ["empty", "file", "symlink"])
def test_custom_output_refuses_every_existing_unowned_kind(
    tmp_path: Path,
    minimal_config: str,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    paths = prepared_project(project, minimal_config)
    output = tmp_path / "output"
    if kind == "empty":
        output.mkdir()
    elif kind == "file":
        output.write_text("keep", encoding="utf-8")
    else:
        target = tmp_path / "target"
        target.mkdir()
        try:
            os.symlink(target, output)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks are unavailable")
    monkeypatch.setattr(
        "tokenmaxxing.profile.build.render_site",
        lambda *args, **kwargs: pytest.fail("renderer must not run"),
    )

    with pytest.raises(ValueError, match="not owned|directory, not a symlink or file"):
        build_profile(
            paths.config,
            db_path=project / "usage.sqlite3",
            output=output,
        )

    assert output.exists() or output.is_symlink()


def test_custom_output_can_be_rebuilt_only_after_tokenmaxxing_owns_it(
    tmp_path: Path, minimal_config: str
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    paths = prepared_project(project, minimal_config)
    output = tmp_path / "public_html"

    first = build_profile(
        paths.config,
        db_path=project / "usage.sqlite3",
        output=output,
        now=datetime(2026, 8, 29, 12, tzinfo=ZoneInfo("UTC")),
    )
    second = build_profile(
        paths.config,
        db_path=project / "usage.sqlite3",
        output=output,
        now=datetime(2026, 8, 30, 12, tzinfo=ZoneInfo("UTC")),
    )

    marker = json.loads((output / ".tokenmaxxing-profile.json").read_text())
    assert marker == {"kind": "tokenmaxxing-profile", "schema_version": 1}
    assert str(tmp_path) not in json.dumps(marker)
    assert first.site_dir == second.site_dir == output


def test_failed_custom_rebuild_preserves_the_owned_site(
    tmp_path: Path, minimal_config: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    paths = prepared_project(project, minimal_config)
    output = tmp_path / "public_html"
    build_profile(
        paths.config,
        db_path=project / "usage.sqlite3",
        output=output,
        now=datetime(2026, 8, 29, 12, tzinfo=ZoneInfo("UTC")),
    )
    before = {
        path.relative_to(output): path.read_bytes()
        for path in output.rglob("*")
        if path.is_file()
    }

    def raising_renderer(*args, **kwargs):
        raise RuntimeError("render failed")

    monkeypatch.setattr("tokenmaxxing.profile.build.render_site", raising_renderer)

    with pytest.raises(RuntimeError, match="render failed"):
        build_profile(
            paths.config,
            db_path=project / "usage.sqlite3",
            output=output,
        )

    after = {
        path.relative_to(output): path.read_bytes()
        for path in output.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_custom_output_requires_both_ownership_and_a_valid_prior_site(
    tmp_path: Path, minimal_config: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    paths = prepared_project(project, minimal_config)
    output = tmp_path / "public_html"
    output.mkdir()
    marker = output / ".tokenmaxxing-profile.json"
    marker.write_text(
        '{"kind":"tokenmaxxing-profile","schema_version":1}\n',
        encoding="utf-8",
    )
    sentinel = output / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(
        "tokenmaxxing.profile.build.render_site",
        lambda *args, **kwargs: pytest.fail("renderer must not run"),
    )

    with pytest.raises(ValueError, match="not a valid prior site"):
        build_profile(
            paths.config,
            db_path=project / "usage.sqlite3",
            output=output,
        )

    assert marker.is_file()
    assert sentinel.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize(
    "marker",
    [
        {"kind": "tokenmaxxing-profile", "schema_version": True},
        {"kind": "other", "schema_version": 1},
        {
            "kind": "tokenmaxxing-profile",
            "schema_version": 1,
            "source_path": "/Users/ada/private",
        },
    ],
)
def test_ownership_marker_has_a_strict_private_path_free_schema(
    tmp_path: Path, marker: dict[str, object]
) -> None:
    write_valid_site(tmp_path)
    (tmp_path / ".tokenmaxxing-profile.json").write_text(
        json.dumps(marker), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="ownership marker"):
        validate_site(tmp_path, noindex=True)


def test_successful_build_replaces_previous_site(
    tmp_path: Path, minimal_config: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = prepared_project(tmp_path, minimal_config)
    previous = write_previous_site(paths.site)

    def renderer(config, data, profile_paths, destination, *, noindex):
        assert profile_paths == paths
        write_valid_site(destination, noindex=noindex)

    monkeypatch.setattr("tokenmaxxing.profile.build.render_site", renderer)

    result = build_profile(
        paths.config,
        db_path=tmp_path / "usage.sqlite3",
        now=datetime(2026, 8, 30, 12, tzinfo=ZoneInfo("UTC")),
    )

    assert result.site_dir == paths.site
    assert result.generated_at.isoformat() == "2026-08-30T12:00:00+00:00"
    assert result.file_count == 8
    assert result.total_bytes > 0
    assert (paths.site / "index.html").read_text(encoding="utf-8") != previous
    assert not list(paths.site.parent.glob(f".{paths.site.name}-backup-*"))


def test_missing_database_fails_without_creating_it(
    tmp_path: Path, minimal_config: str
) -> None:
    paths = prepared_project(tmp_path, minimal_config)
    database = tmp_path / "usage.sqlite3"
    database.unlink()

    with pytest.raises(FileNotFoundError, match="usage database does not exist"):
        build_profile(paths.config, db_path=database)

    assert not database.exists()


def test_failed_render_preserves_previous_site(
    tmp_path: Path, minimal_config: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = prepared_project(tmp_path, minimal_config)
    previous = write_previous_site(paths.site)

    def raising_renderer(*args, **kwargs):
        raise RuntimeError("render failed")

    monkeypatch.setattr("tokenmaxxing.profile.build.render_site", raising_renderer)
    with pytest.raises(RuntimeError, match="render failed"):
        build_profile(paths.config, db_path=tmp_path / "usage.sqlite3")
    assert (paths.site / "index.html").read_text(encoding="utf-8") == previous


def test_profile_json_schema_accepts_optional_awards(tmp_path: Path) -> None:
    write_valid_site(tmp_path)
    payload = public_payload(awards=False)
    (tmp_path / "profile.json").write_text(json.dumps(payload), encoding="utf-8")

    validate_site(tmp_path, noindex=True)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda payload: payload.update({"schema_version": True}), "schema_version"),
        (
            lambda payload: payload.update({"generated_at": "2026-08-30T12:00:00"}),
            "timezone",
        ),
        (
            lambda payload: payload["profile"]["links"][0].update(
                {"url": "https:missing-host"}
            ),
            "absolute https",
        ),
        (
            lambda payload: payload["profile"]["links"][0].update({"url": "mailto:"}),
            "address",
        ),
        (
            lambda payload: payload["stats"]["awards"][0].update(
                {"earned_on": "August 21"}
            ),
            "ISO date",
        ),
    ],
)
def test_profile_json_schema_uses_strict_public_types(
    tmp_path: Path, mutation, message: str
) -> None:
    write_valid_site(tmp_path)
    payload = public_payload()
    mutation(payload)
    (tmp_path / "profile.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        validate_site(tmp_path, noindex=True)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda payload: payload.update({"schema_version": 2}), "schema_version"),
        (lambda payload: payload.update({"unexpected": True}), "unknown public field"),
        (
            lambda payload: payload["stats"].update({"session_id": "private"}),
            "forbidden public field",
        ),
        (
            lambda payload: payload["profile"].update({"bio": "/Users/ada/private"}),
            "local path",
        ),
    ],
)
def test_profile_json_rejects_invalid_or_private_fields(
    tmp_path: Path, mutation, message: str
) -> None:
    write_valid_site(tmp_path)
    payload = public_payload()
    mutation(payload)
    (tmp_path / "profile.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        validate_site(tmp_path, noindex=True)


@pytest.mark.parametrize(
    "private_value",
    [
        "/private/var/folders/tokenmaxxing.sqlite3",
        "/tmp/tokenmaxxing.sqlite3",
        "/root/.local/share/tokenmaxxing",
        r"C:\Users\Ada\tokenmaxxing.sqlite3",
        r"\\server\share\tokenmaxxing.sqlite3",
        r"\\?\C:\private\tokenmaxxing.sqlite3",
        "../private/tokenmaxxing.sqlite3",
        "~/.local/share/tokenmaxxing",
    ],
)
def test_profile_json_rejects_platform_private_paths(
    tmp_path: Path, private_value: str
) -> None:
    write_valid_site(tmp_path)
    payload = public_payload()
    payload["profile"]["bio"] = private_value
    (tmp_path / "profile.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="local path"):
        validate_site(tmp_path, noindex=True)


def test_visible_totals_must_match_profile_json(tmp_path: Path) -> None:
    write_valid_site(tmp_path)
    index = tmp_path / "index.html"
    index.write_text(
        index.read_text(encoding="utf-8").replace(
            'data-total-tokens="0"', 'data-total-tokens="10"'
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="visible total"):
        validate_site(tmp_path, noindex=True)


@pytest.mark.parametrize(
    "relative_path",
    [
        "assets/profile.css",
        "assets/profile.js",
        "robots.txt",
        "profile.json",
        "index.html",
    ],
)
def test_required_files_must_exist(tmp_path: Path, relative_path: str) -> None:
    write_valid_site(tmp_path)
    (tmp_path / relative_path).unlink()

    with pytest.raises(ValueError, match="required site file"):
        validate_site(tmp_path, noindex=True)


@pytest.mark.parametrize(
    ("html_fragment", "message"),
    [
        ('<script src="https://cdn.example/app.js"></script>', "remote asset"),
        ('<img src="//cdn.example/avatar.png">', "remote asset"),
        ('<img srcset="https://cdn.example/avatar.png 1x">', "remote asset"),
        ('<object data="https://cdn.example/document"></object>', "remote asset"),
        ('<svg><use href="https://cdn.example/icons.svg#x"></use></svg>', "remote asset"),
        ('<script src="../outside.js"></script>', "escapes site"),
        ('<script src="assets/missing.js"></script>', "missing local asset"),
    ],
)
def test_html_rejects_unsafe_asset_references(
    tmp_path: Path, html_fragment: str, message: str
) -> None:
    write_valid_site(tmp_path)
    index = tmp_path / "index.html"
    index.write_text(
        index.read_text(encoding="utf-8").replace("</body>", html_fragment + "</body>"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=message):
        validate_site(tmp_path, noindex=True)


@pytest.mark.parametrize(
    ("html_fragment", "message"),
    [
        ('<base href="https://cdn.example/">', "base"),
        ('<a href="https://unconfigured.example/">outside</a>', "configured public URL"),
        (
            '<form action="https://unconfigured.example/submit"></form>',
            "configured public URL",
        ),
        ('<svg><image href="https://cdn.example/image.png"></image></svg>', "remote asset"),
        ('<div style="background:url(https://cdn.example/image.png)"></div>', "CSS asset"),
        ('<meta http-equiv="refresh" content="0; url=https://example.net/">', "refresh"),
        ('<link rel="prefetch" href="https://cdn.example/next.js">', "remote asset"),
        (
            '<script type="module">import "https://cdn.example/module.js";</script>',
            "inline executable",
        ),
        ('<script>const localOnly = true;</script>', "inline executable"),
    ],
)
def test_html_rejects_remote_loading_and_navigation_bypasses(
    tmp_path: Path, html_fragment: str, message: str
) -> None:
    write_valid_site(tmp_path)
    index = tmp_path / "index.html"
    index.write_text(
        index.read_text(encoding="utf-8").replace("<body", html_fragment + "<body"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=message):
        validate_site(tmp_path, noindex=True)


@pytest.mark.parametrize(
    ("html_fragment", "message"),
    [
        (
            "<body onload=\"globalThis['fe'+'tch']('ht'+'tps://example.invalid/collect')\">",
            "event-handler attribute",
        ),
        ('<button ONCLICK="">safe label</button>', "event-handler attribute"),
        ("<div oncustom></div>", "event-handler attribute"),
        (
            '<iframe srcdoc="&lt;img src=\'https://example.invalid/pixel\'&gt;"></iframe>',
            "srcdoc",
        ),
        ('<iframe SRCDOC=""></iframe>', "srcdoc"),
    ],
)
def test_html_rejects_executable_and_nested_markup_attributes(
    tmp_path: Path, html_fragment: str, message: str
) -> None:
    write_valid_site(tmp_path)
    index = tmp_path / "index.html"
    index.write_text(
        index.read_text(encoding="utf-8").replace(
            "</body>", html_fragment + "</body>"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=message):
        validate_site(tmp_path, noindex=True)


def test_inline_style_attributes_keep_strict_css_validation(tmp_path: Path) -> None:
    write_valid_site(tmp_path)
    index = tmp_path / "index.html"
    original = index.read_text(encoding="utf-8")
    index.write_text(
        original.replace(
            "</body>",
            '<div style="background:image-set(\'https://example.invalid/a.png\' 1x)"></div>'
            "</body>",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="inline CSS asset"):
        validate_site(tmp_path, noindex=True)


def test_local_svg_must_not_embed_remote_assets(tmp_path: Path) -> None:
    write_valid_site(tmp_path)
    (tmp_path / "assets" / "remote.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"><image href="https://cdn.example/image.png"/></svg>',
        encoding="utf-8",
    )
    index = tmp_path / "index.html"
    index.write_text(
        index.read_text(encoding="utf-8").replace(
            "</body>", '<img src="assets/remote.svg" alt=""></body>'
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="SVG asset"):
        validate_site(tmp_path, noindex=True)


@pytest.mark.parametrize(
    "css",
    [
        '@import "https://cdn.example/theme.css";',
        "body { background: url(//cdn.example/bg.png); }",
        "body { background: url(../../private.png); }",
        "body { background: url(data:image/svg+xml;base64,PHN2Zz4=); }",
    ],
)
def test_css_rejects_remote_and_escaping_assets(tmp_path: Path, css: str) -> None:
    write_valid_site(tmp_path)
    (tmp_path / "assets" / "profile.css").write_text(css, encoding="utf-8")

    with pytest.raises(ValueError, match="CSS asset"):
        validate_site(tmp_path, noindex=True)


@pytest.mark.parametrize(
    "css",
    [
        'body { background: image-set("https://cdn.example/a.png" 1x); }',
        'body { background: -webkit-image-set("assets/a.png" 1x); }',
        'body { background: src("assets/a.png"); }',
        'body::before { content: "https://cdn.example/a.png"; }',
    ],
)
def test_packaged_css_rejects_non_url_resource_loading_constructs(
    tmp_path: Path, css: str
) -> None:
    write_valid_site(tmp_path)
    (tmp_path / "assets" / "profile.css").write_text(css, encoding="utf-8")

    with pytest.raises(ValueError, match="packaged CSS"):
        validate_site(tmp_path, noindex=True)


def test_packaged_css_allows_validated_local_url_references(tmp_path: Path) -> None:
    write_valid_site(tmp_path)
    (tmp_path / "assets" / "local.png").write_bytes(b"local")
    (tmp_path / "assets" / "profile.css").write_text(
        'body { background: url("local.png"); }\n', encoding="utf-8"
    )

    validate_site(tmp_path, noindex=True)


@pytest.mark.parametrize(
    "css",
    [
        '@import "local.css";',
        'body { background: url("local.png"); }',
        'body { background: image-set("local.png" 1x); }',
        'body { background: -webkit-image-set("local.png" 1x); }',
        'body { background: src("local.png"); }',
    ],
)
def test_custom_css_cannot_load_any_resources(tmp_path: Path, css: str) -> None:
    write_valid_site(tmp_path)
    (tmp_path / "assets" / "custom.css").write_text(css, encoding="utf-8")

    with pytest.raises(ValueError, match="custom CSS cannot load resources"):
        validate_site(tmp_path, noindex=True)


def test_custom_css_without_resource_loading_is_allowed(tmp_path: Path) -> None:
    write_valid_site(tmp_path)
    (tmp_path / "assets" / "custom.css").write_text(
        "body { color: rebeccapurple; }\n", encoding="utf-8"
    )

    validate_site(tmp_path, noindex=True)


@pytest.mark.parametrize(
    "javascript",
    [
        "fetch('ht' + 'tps://example.com/data');",
        "new XMLHttpRequest();",
        "new WebSocket(endpoint);",
        "new EventSource(endpoint);",
        "navigator.sendBeacon(endpoint);",
        "importScripts(workerPath);",
        "import(modulePath);",
        "import /* disguised */ (modulePath);",
    ],
)
def test_javascript_assets_reject_network_capable_tokens(
    tmp_path: Path, javascript: str
) -> None:
    write_valid_site(tmp_path)
    (tmp_path / "assets" / "profile.js").write_text(javascript, encoding="utf-8")

    with pytest.raises(ValueError, match="network-capable JavaScript token"):
        validate_site(tmp_path, noindex=True)


def test_javascript_assets_still_reject_literal_remote_references(tmp_path: Path) -> None:
    write_valid_site(tmp_path)
    (tmp_path / "assets" / "profile.js").write_text(
        "const endpoint = 'https://example.com/data';", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="remote executable reference"):
        validate_site(tmp_path, noindex=True)


def test_recursive_site_walk_rejects_symlinks(tmp_path: Path) -> None:
    write_valid_site(tmp_path)
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    try:
        os.symlink(outside, tmp_path / "assets" / "linked.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable")

    with pytest.raises(ValueError, match="symlink"):
        validate_site(tmp_path, noindex=True)


def test_indexability_metadata_must_match_mode(tmp_path: Path) -> None:
    write_valid_site(tmp_path, noindex=False)
    validate_site(tmp_path, noindex=False)
    index = tmp_path / "index.html"
    index.write_text(
        index.read_text(encoding="utf-8").replace("index, follow", "noindex, nofollow"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="sitemap"):
        validate_site(tmp_path, noindex=True)


def test_noindex_robots_file_must_disallow_everything(tmp_path: Path) -> None:
    write_valid_site(tmp_path)
    (tmp_path / "robots.txt").write_text(
        "User-agent: *\nAllow: /\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="robots.txt"):
        validate_site(tmp_path, noindex=True)


def test_indexable_discovery_files_must_match_canonical_url(tmp_path: Path) -> None:
    write_valid_site(tmp_path, noindex=False)
    (tmp_path / "sitemap.xml").write_text(
        '<?xml version="1.0"?><urlset><url><loc>https://wrong.example/</loc></url></urlset>',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="sitemap"):
        validate_site(tmp_path, noindex=False)


def test_compressed_asset_budgets_are_enforced(tmp_path: Path) -> None:
    write_valid_site(tmp_path)
    noise = os.urandom(25_000)
    (tmp_path / "assets" / "profile.js").write_bytes(noise)

    with pytest.raises(ValueError, match="compressed JavaScript"):
        validate_site(tmp_path, noindex=True)


def test_javascript_budget_includes_every_generated_script(tmp_path: Path) -> None:
    write_valid_site(tmp_path)
    (tmp_path / "assets" / "extra.js").write_bytes(os.urandom(25_000))
    index = tmp_path / "index.html"
    index.write_text(
        index.read_text(encoding="utf-8").replace(
            "</body>", '<script src="assets/extra.js"></script></body>'
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="compressed JavaScript"):
        validate_site(tmp_path, noindex=True)


def test_destination_symlink_or_file_is_rejected(
    tmp_path: Path, minimal_config: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = prepared_project(tmp_path, minimal_config)
    paths.site.parent.mkdir(parents=True)
    paths.site.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(
        "tokenmaxxing.profile.build.render_site",
        lambda *args, **kwargs: pytest.fail("renderer must not run"),
    )

    with pytest.raises(ValueError, match="destination must be a directory"):
        build_profile(paths.config, db_path=tmp_path / "usage.sqlite3")


def test_windows_permission_error_on_first_rename_preserves_site(
    tmp_path: Path, minimal_config: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = prepared_project(tmp_path, minimal_config)
    previous = write_previous_site(paths.site)
    monkeypatch.setattr(
        "tokenmaxxing.profile.build.render_site",
        lambda config, data, profile_paths, destination, *, noindex: write_valid_site(
            destination, noindex=noindex
        ),
    )

    def denied(source: Path, destination: Path) -> None:
        raise PermissionError("open handle")

    monkeypatch.setattr("tokenmaxxing.profile.build._rename_directory", denied)

    with pytest.raises(PermissionError, match="open handle"):
        build_profile(paths.config, db_path=tmp_path / "usage.sqlite3")
    assert (paths.site / "index.html").read_text(encoding="utf-8") == previous


def test_second_rename_failure_restores_backup(
    tmp_path: Path, minimal_config: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = prepared_project(tmp_path, minimal_config)
    previous = write_previous_site(paths.site)
    monkeypatch.setattr(
        "tokenmaxxing.profile.build.render_site",
        lambda config, data, profile_paths, destination, *, noindex: write_valid_site(
            destination, noindex=noindex
        ),
    )
    real_rename = Path.rename
    calls = 0

    def fail_second(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PermissionError("new site locked")
        real_rename(source, destination)

    monkeypatch.setattr("tokenmaxxing.profile.build._rename_directory", fail_second)

    with pytest.raises(PermissionError, match="new site locked"):
        build_profile(paths.config, db_path=tmp_path / "usage.sqlite3")
    assert (paths.site / "index.html").read_text(encoding="utf-8") == previous
    assert calls == 3


def test_restore_failure_retains_backup_and_reports_exact_path(
    tmp_path: Path, minimal_config: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = prepared_project(tmp_path, minimal_config)
    previous = write_previous_site(paths.site)
    monkeypatch.setattr(
        "tokenmaxxing.profile.build.render_site",
        lambda config, data, profile_paths, destination, *, noindex: write_valid_site(
            destination, noindex=noindex
        ),
    )
    real_rename = Path.rename
    calls = 0

    def fail_replace_and_restore(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise PermissionError(f"rename {calls} failed")
        real_rename(source, destination)

    monkeypatch.setattr(
        "tokenmaxxing.profile.build._rename_directory", fail_replace_and_restore
    )

    with pytest.raises(BuildRecoveryError) as captured:
        build_profile(paths.config, db_path=tmp_path / "usage.sqlite3")

    backups = list(paths.site.parent.glob(f".{paths.site.name}-backup-*"))
    assert len(backups) == 1
    assert str(backups[0]) in str(captured.value)
    assert (backups[0] / "index.html").read_text(encoding="utf-8") == previous


def test_backup_cleanup_failure_keeps_successful_site_live(
    tmp_path: Path, minimal_config: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = prepared_project(tmp_path, minimal_config)
    previous = write_previous_site(paths.site)
    monkeypatch.setattr(
        "tokenmaxxing.profile.build.render_site",
        lambda config, data, profile_paths, destination, *, noindex: write_valid_site(
            destination, noindex=noindex
        ),
    )

    def fail_cleanup(path: Path) -> None:
        raise PermissionError("backup locked")

    monkeypatch.setattr("tokenmaxxing.profile.build._remove_directory", fail_cleanup)

    with pytest.warns(BuildCleanupWarning, match="retained previous site backup"):
        result = build_profile(paths.config, db_path=tmp_path / "usage.sqlite3")

    assert result.site_dir == paths.site
    assert (paths.site / "index.html").read_text(encoding="utf-8") != previous
    assert len(list(paths.site.parent.glob(f".{paths.site.name}-backup-*"))) == 1
