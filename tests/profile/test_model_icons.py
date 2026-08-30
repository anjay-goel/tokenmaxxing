import pytest

from tokenmaxxing.profile.model_icons import canonical_creator, model_icon


@pytest.mark.parametrize(
    ("provider", "creator"),
    (
        ("openai-codex", "openai"),
        ("claude", "anthropic"),
        ("google-generative-ai", "google"),
        ("z.ai", "zai"),
        ("zhipuai", "zai"),
        ("moonshotai", "moonshot"),
        ("x.ai", "xai"),
        ("databricks", "dbrx"),
        ("01.ai", "yi"),
    ),
)
def test_creator_provider_aliases_are_canonical(provider: str, creator: str) -> None:
    assert canonical_creator(provider) == creator
    assert canonical_creator(creator) == creator


@pytest.mark.parametrize(
    "provider",
    (
        "opencode",
        "openrouter",
        "ollama",
        "vllm",
        "lmstudio",
        "groq",
        "fireworks",
        "together",
        "dashscope",
        "vertexai",
    ),
)
def test_host_and_harness_providers_are_not_model_creators(provider: str) -> None:
    assert canonical_creator(provider) is None


@pytest.mark.parametrize(
    ("model", "icon"),
    (
        ("deepseek-r1-distill-qwen-32b", "deepseek"),
        ("qwen2.5-coder", "qwen"),
        ("codellama-70b", "meta"),
        ("mixtral-8x22b", "mistral"),
        ("qwq-32b", "qwen"),
        ("o3-pro", "openai"),
        ("skywork-o1", "skywork"),
    ),
)
def test_specific_model_family_wins_before_nested_or_short_names(
    model: str, icon: str
) -> None:
    assert model_icon(None, model) == icon


@pytest.mark.parametrize("model", ("unknown", "unknown model", "(unknown)", "<unknown>"))
def test_unknown_model_uses_neutral_icon_even_with_known_provider(model: str) -> None:
    assert model_icon("openai", model) == "generic"
