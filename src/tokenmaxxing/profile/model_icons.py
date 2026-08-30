from __future__ import annotations

import re


_CREATOR_ALIASES = {
    "01.ai": "yi",
    "01ai": "yi",
    "ai2": "ai2",
    "ai21": "ai21",
    "ai360": "ai360",
    "allenai": "ai2",
    "anthropic": "anthropic",
    "baichuan": "baichuan",
    "baidu": "baidu",
    "chatglm": "chatglm",
    "claude": "anthropic",
    "cohere": "cohere",
    "databricks": "dbrx",
    "dbrx": "dbrx",
    "deepseek": "deepseek",
    "doubao": "doubao",
    "google": "google",
    "google-generative-ai": "google",
    "huggingface": "huggingface",
    "hunyuan": "hunyuan",
    "ibm": "ibm",
    "internlm": "internlm",
    "kimi": "kimi",
    "liquid": "liquid",
    "meta": "meta",
    "microsoft": "microsoft",
    "minimax": "minimax",
    "mistral": "mistral",
    "moonshot": "moonshot",
    "moonshotai": "moonshot",
    "nvidia": "nvidia",
    "openai": "openai",
    "openai-codex": "openai",
    "openai_codex": "openai",
    "perplexity": "perplexity",
    "rwkv": "rwkv",
    "sensenova": "sensenova",
    "skywork": "skywork",
    "snowflake": "snowflake",
    "stepfun": "stepfun",
    "tii": "tii",
    "wenxin": "wenxin",
    "x.ai": "xai",
    "xai": "xai",
    "xiaomimimo": "xiaomimimo",
    "yi": "yi",
    "z.ai": "zai",
    "z-ai": "zai",
    "zai": "zai",
    "zhipu": "zai",
    "zhipuai": "zai",
}

_CREATOR_ICONS = {
    "anthropic": "claude",
    **{creator: creator for creator in set(_CREATOR_ALIASES.values()) - {"anthropic"}},
}

_EDGE = r"(?:^|[/_.:-])"
_END = r"(?:$|[/_.:-]|\d)"
_MODEL_FAMILIES = tuple(
    (icon, re.compile(pattern))
    for icon, pattern in (
        ("openai", rf"{_EDGE}(?:gpt|codex){_END}"),
        ("claude", rf"{_EDGE}claude{_END}"),
        ("google", rf"{_EDGE}(?:gemini|palm){_END}"),
        ("gemma", rf"{_EDGE}gemma{_END}"),
        ("deepseek", rf"{_EDGE}deepseek{_END}"),
        ("chatglm", rf"{_EDGE}chatglm{_END}"),
        ("zai", rf"{_EDGE}(?:glm|codegeex){_END}"),
        ("kimi", rf"{_EDGE}kimi{_END}"),
        ("moonshot", rf"{_EDGE}moonshot{_END}"),
        ("xai", rf"{_EDGE}grok{_END}"),
        (
            "mistral",
            rf"{_EDGE}(?:mistral|mixtral|codestral|ministral|pixtral){_END}",
        ),
        ("qwen", rf"{_EDGE}(?:qwen|qwq|qvq){_END}"),
        ("meta", rf"{_EDGE}(?:llama|codellama){_END}"),
        ("cohere", rf"{_EDGE}(?:command[-_.:]?[ra]|aya){_END}"),
        ("minimax", rf"{_EDGE}(?:minimax|abab){_END}"),
        ("baichuan", rf"{_EDGE}baichuan{_END}"),
        ("yi", rf"{_EDGE}yi{_END}"),
        ("hunyuan", rf"{_EDGE}hunyuan{_END}"),
        ("doubao", rf"{_EDGE}doubao{_END}"),
        ("internlm", rf"{_EDGE}internlm{_END}"),
        ("microsoft", rf"{_EDGE}phi{_END}"),
        ("wenxin", rf"{_EDGE}(?:ernie|wenxin){_END}"),
        ("nvidia", rf"{_EDGE}nemotron{_END}"),
        ("stepfun", rf"{_EDGE}(?:stepfun|step[-_.:]?\d){_END}"),
        ("ibm", rf"{_EDGE}granite{_END}"),
        ("ai2", rf"{_EDGE}olmo{_END}"),
        ("tii", rf"{_EDGE}falcon{_END}"),
        ("rwkv", rf"{_EDGE}rwkv{_END}"),
        ("dbrx", rf"{_EDGE}dbrx{_END}"),
        ("huggingface", rf"{_EDGE}smollm{_END}"),
        ("sensenova", rf"{_EDGE}sensenova{_END}"),
        ("skywork", rf"{_EDGE}skywork{_END}"),
        ("xiaomimimo", rf"{_EDGE}mimo{_END}"),
        ("ai21", rf"{_EDGE}jamba{_END}"),
        ("longcat", rf"{_EDGE}longcat{_END}"),
        ("ai360", rf"{_EDGE}(?:zhinao|360gpt){_END}"),
        ("spark", rf"{_EDGE}sparkdesk{_END}"),
        ("bedrock", rf"{_EDGE}(?:amazon[-_.:]nova|nova[-_.:](?:pro|lite|micro|premier)){_END}"),
        ("perplexity", rf"{_EDGE}sonar{_END}"),
        ("liquid", rf"{_EDGE}lfm{_END}"),
        ("snowflake", rf"{_EDGE}snowflake[-_.:]arctic{_END}"),
        ("openai", rf"{_EDGE}o[134](?:$|[/_.:-])"),
    )
)


def canonical_creator(provider: str | None) -> str | None:
    if not isinstance(provider, str):
        return None
    return _CREATOR_ALIASES.get(provider.strip().lower())


def model_icon(provider: str | None, model: str) -> str:
    normalized = model.strip().lower()
    if normalized in {"unknown", "unknown model", "unknown models", "(unknown)", "<unknown>"}:
        return "generic"
    creator = canonical_creator(provider)
    if creator is not None:
        return _CREATOR_ICONS[creator]
    for icon, pattern in _MODEL_FAMILIES:
        if pattern.search(normalized):
            return icon
    return "generic"
