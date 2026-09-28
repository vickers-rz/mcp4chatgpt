from __future__ import annotations

import hashlib

import pytest

from mcp4chatgpt import skill_resources


def test_skill_catalog_frontmatter_and_digest_match_resource_bytes():
    listed = skill_resources.list_skills()
    assert list(listed) == ["skills"]
    assert len(listed["skills"]) == 1

    skill = listed["skills"][0]
    assert skill["uri"] == skill_resources.SKILL_URI
    assert skill["frontmatter"] == {
        "name": "local-web-access",
        "description": (
            "Use MCP4ChatGPT local Chrome / 本机 Chrome for URL reading or web "
            "search when the user asks for the local browser, current logged-in "
            "session / 当前登录态, 4GPT or read_webpage, or when cloud web access "
            "failed, was blocked, or returned no usable body."
        ),
    }
    assert len(skill["resources"]) == 1

    resource = skill_resources.read_resource(skill["uri"])["contents"][0]
    assert resource["uri"] == skill["uri"]
    assert resource["mimeType"] == "text/markdown"
    digest = "sha256:" + hashlib.sha256(resource["text"].encode("utf-8")).hexdigest()
    assert skill["resources"][0] == {"uri": skill["uri"], "digest": digest}
    assert skill["frontmatter"] == skill_resources._parse_frontmatter(resource["text"])


def test_skills_get_matches_list_entry_and_returns_defensive_copies():
    listed = skill_resources.list_skills()["skills"][0]
    fetched = skill_resources.get_skill(skill_resources.SKILL_URI)["skill"]

    assert fetched == listed
    fetched["frontmatter"]["name"] = "mutated"
    assert skill_resources.list_skills()["skills"][0]["frontmatter"]["name"] == "local-web-access"


@pytest.mark.parametrize(
    "uri",
    [
        "skill://mcp4chatgpt/other/SKILL.md",
        "skill://mcp4chatgpt/local-web-access/../SKILL.md",
        "skill://other/local-web-access/SKILL.md",
        "skill://mcp4chatgpt/local-web-access/SKILL.md?x=1",
        "skill://mcp4chatgpt/local-web-access/SKILL.md#fragment",
    ],
)
def test_skill_uri_validation_rejects_aliases_and_normalization_tricks(uri):
    with pytest.raises(ValueError, match="Unknown skill resource"):
        skill_resources.get_skill(uri)
    with pytest.raises(ValueError, match="Unknown skill resource"):
        skill_resources.read_resource(uri)


def test_single_page_skill_catalog_rejects_unissued_cursor():
    assert "nextCursor" not in skill_resources.list_skills()
    with pytest.raises(ValueError, match="Invalid skills cursor"):
        skill_resources.list_skills("not-issued")
