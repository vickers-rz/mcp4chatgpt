from __future__ import annotations

from pathlib import Path

from mcp4chatgpt import skill_resources


ROOT = Path(__file__).resolve().parents[1]
PERSONAL_SKILL = ROOT / "chatgpt_skills" / "local-web-access" / "SKILL.md"
OPENAI_YAML = ROOT / "chatgpt_skills" / "local-web-access" / "agents" / "openai.yaml"


def test_personal_skill_frontmatter_and_activation_contract():
    text = PERSONAL_SKILL.read_text(encoding="utf-8")
    frontmatter = skill_resources._parse_frontmatter(text)

    assert frontmatter["name"] == "local-web-access"
    description = frontmatter["description"]
    for trigger in (
        "4GPT",
        "local Chrome",
        "本机 Chrome",
        "当前登录态",
        "read_webpage",
        "cloud web access failed",
    ):
        assert trigger in description

    assert "Do not activate this skill for ordinary web research" in text


def test_personal_skill_openai_manifest_uses_supported_chat_product():
    text = OPENAI_YAML.read_text(encoding="utf-8")

    assert "display_name: local web access" in text
    assert "default_prompt:" in text
    assert "products:\n  - CHAT" in text
    assert "- chatgpt" not in text.casefold()
    assert "allow_implicit_invocation: true" in text
    assert "type: mcp" in text
    assert "value: mcp4chatgpt" in text
    assert "transport: streamable_http" in text
    assert "url: https://mcp.runzhe.uk/mcp" in text


def test_personal_and_server_skills_keep_core_routing_contract_in_sync():
    personal = PERSONAL_SKILL.read_text(encoding="utf-8")
    server = skill_resources.SKILL_MD

    required_contract = (
        "local-web-access",
        "本机 Chrome",
        "read_webpage",
        "search_web",
        "backend=browser",
        "capability_search",
        "challenge",
        "access_blocked",
        "Do not activate this skill for ordinary web research",
    )
    for marker in required_contract:
        assert marker in personal
        assert marker in server
