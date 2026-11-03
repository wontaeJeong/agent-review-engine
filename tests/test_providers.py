from __future__ import annotations

import httpx
import pytest

from agent_review_engine.config import Settings
from agent_review_engine.providers import JevClassifier, LLMReviewer
from agent_review_engine.policy import fallback_labels


def sample_file():
    return {"filename": "src/service.py", "patch": "@@ -1 +1 @@\n-a()\n+b()", "changes": 2}


@pytest.mark.asyncio
async def test_jev_disabled_does_not_make_http_request():
    async def transport(request):
        raise AssertionError("Jev must never be called when disabled")

    config = Settings(jev_enabled=False)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        jev = JevClassifier(config, client)
        with pytest.raises(RuntimeError, match="disabled"):
            await jev.classify(title="a", description="b", files=[sample_file()])
    assert fallback_labels(["docs/readme.md"]).risk == "low"


@pytest.mark.asyncio
async def test_jev_local_schema_parsed():
    def transport(request):
        assert str(request.url) == "http://jev.internal/v1/systemone"
        return httpx.Response(200, json={"answers": {
            "risk": {"choice": "medium", "confidence": .9},
            "area": {"choice": "backend", "confidence": .8},
        }})

    config = Settings(jev_enabled=True, jev_api_url="http://jev.internal/v1/systemone")
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        result = await JevClassifier(config, client).classify(title="a", description="b", files=[sample_file()])
    assert (result.risk, result.area, result.confidence) == ("medium", "backend", .8)


@pytest.mark.asyncio
async def test_llm_url_and_key_are_separate_and_json_validated():
    def transport(request):
        assert str(request.url) == "http://local.test/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer internal-key"
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"verdict":"approve","summary":"Looks fine","findings":[]}'}}],
            "usage": {"prompt_tokens": 23, "completion_tokens": 11}})

    config = Settings(llm_base_url="http://local.test/v1", llm_api_key="internal-key", llm_model="mock")
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        verdict, usage = await LLMReviewer(config, client).review(
            title="Change", description="fix", files=[sample_file()], ci_summary="passed", phase="pr")
    assert verdict.verdict == "approve" and usage["prompt_tokens"] == 23


@pytest.mark.asyncio
async def test_insufficient_or_partial_diff_never_approves():
    config = Settings(llm_base_url="http://local.test/v1", llm_model="mock")
    async with httpx.AsyncClient() as client:
        reviewer = LLMReviewer(config, client)
        for files in ([], [{"filename": "binary.dat", "patch": None}]):
            with pytest.raises(ValueError):
                await reviewer.review(title="a", description="b", files=files,
                                      ci_summary="passed", phase="pr")


@pytest.mark.asyncio
async def test_jev_low_confidence_falls_back_in_engine():
    def transport(request):
        return httpx.Response(200, json={"answers": {
            "risk": {"choice": "low", "confidence": .1},
            "area": {"choice": "docs", "confidence": .1}}})
    config = Settings(jev_enabled=True, jev_api_url="http://jev.local/v1/systemone",
                      jev_min_confidence=.7)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(ValueError, match="confidence"):
            await JevClassifier(config, client).classify(
                title="Docs", description="short", files=[sample_file()])


@pytest.mark.asyncio
async def test_llm_rejects_approve_with_medium_finding():
    def transport(request):
        return httpx.Response(200, json={"choices": [{"message": {"content":
            '{"verdict":"approve","summary":"ok","findings":[{' \
            '"path":"src/service.py","severity":"medium",' \
            '"explanation":"issue","evidence":"example"}]}'}}]})
    config = Settings(llm_base_url="http://llm/v1", llm_model="mock")
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(ValueError, match="Inconsistent"):
            await LLMReviewer(config, client).review(
                title="fix", description="", files=[sample_file()], ci_summary="ok", phase="pr")
