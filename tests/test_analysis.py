from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from conftest import make_opportunity

from jobhunt.ai import (
    AIValidationError,
    AnalysisOrchestrator,
    AnalysisResponse,
    GeminiAnalyzer,
    OllamaAnalyzer,
    ProviderRequestError,
)
from jobhunt.budget import MemoryAnalysisQuotaLedger, QuotaExceeded
from jobhunt.evaluation import evaluate_provider, load_evaluation_fixtures
from jobhunt.models import AIAnalysis, GeminiAnalysisConfig, OllamaAnalysisConfig


def _analysis(record_id: object, claim_id: str) -> AIAnalysis:
    return AIAnalysis(
        record_id=record_id,
        required_skill_score=35,
        evidence_relevance_score=25,
        scope_seniority_score=15,
        matched_evidence_ids=[claim_id],
        rationale="Grounded in the verified workflow project.",
        confidence=0.9,
        draft="I built a relevant workflow dashboard.",
        draft_fact_ids=[claim_id],
    )


def test_ollama_uses_only_local_tags_and_chat_without_downloading(verified_claim) -> None:
    opportunity = make_opportunity(ai_processing_allowed=True)
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3:4b-instruct-2507-q4_K_M"}]})
        payload = json.loads(request.content)
        assert payload["stream"] is False
        assert payload["options"]["temperature"] == 0
        assert payload["format"]["title"] == "AIAnalysis"
        return httpx.Response(
            200,
            json={
                "model": "qwen3:4b-instruct-2507-q4_K_M",
                "message": {
                    "content": _analysis(
                        opportunity.record_id, verified_claim.claim_id
                    ).model_dump_json()
                },
                "prompt_eval_count": 400,
                "eval_count": 120,
            },
        )

    analyzer = OllamaAnalyzer(
        OllamaAnalysisConfig(enabled=True, evaluation_approved=True),
        transport=httpx.MockTransport(handler),
    )
    try:
        response = analyzer.analyze(opportunity, [verified_claim])
    finally:
        analyzer.close()
    assert paths == ["/api/tags", "/api/chat"]
    assert response.provider == "ollama"
    assert response.output_tokens == 120


def test_ollama_missing_model_fails_without_pull(verified_claim) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"models": []})

    analyzer = OllamaAnalyzer(
        OllamaAnalysisConfig(enabled=True, evaluation_approved=True),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(RuntimeError, match="no download"):
            analyzer.analyze(make_opportunity(ai_processing_allowed=True), [verified_claim])
    finally:
        analyzer.close()
    assert paths == ["/api/tags"]


class _Provider:
    provider_id = "fake"
    model_id = "fake-model"
    max_rows_per_run = 20

    def __init__(self, result: AnalysisResponse | Exception) -> None:
        self.result = result

    def analyze(self, opportunity, verified_claims):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def close(self) -> None:
        pass


def test_orchestrator_does_not_fallback_on_schema_error(verified_claim) -> None:
    opportunity = make_opportunity(ai_processing_allowed=True)
    fallback = _Provider(
        AnalysisResponse(
            _analysis(opportunity.record_id, verified_claim.claim_id), "ollama", "fake"
        )
    )
    orchestrator = AnalysisOrchestrator(_Provider(AIValidationError("bad schema")), fallback)
    with pytest.raises(AIValidationError):
        orchestrator.analyze(opportunity, [verified_claim])


def test_daily_quota_fails_closed() -> None:
    ledger = MemoryAnalysisQuotaLedger()
    now = datetime(2026, 9, 2, tzinfo=UTC)
    ledger.reserve(max_rows_per_day=2, now=now)
    ledger.reserve(max_rows_per_day=2, now=now)
    with pytest.raises(QuotaExceeded, match="Daily"):
        ledger.reserve(max_rows_per_day=2, now=now)


def test_model_and_host_allowlists_are_fail_closed() -> None:
    with pytest.raises(ValueError, match="allowlist"):
        OllamaAnalysisConfig(model="another-model")
    with pytest.raises(ValueError, match="restricted"):
        OllamaAnalysisConfig(base_url="http://example.com:11434")


class _GeminiModels:
    def __init__(self, response=None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.observed = None

    def generate_content(self, **kwargs):
        self.observed = kwargs
        if self.error:
            raise self.error
        return self.response


class _GeminiClient:
    def __init__(self, models: _GeminiModels) -> None:
        self.models = models

    def close(self) -> None:
        pass


class _Usage:
    prompt_token_count = 321
    candidates_token_count = 100


class _GeminiResponse:
    def __init__(self, text: str) -> None:
        self.text = text
        self.model_version = "gemini-3.7-flash"
        self.usage_metadata = _Usage()


def _gemini_config() -> GeminiAnalysisConfig:
    return GeminiAnalysisConfig(enabled=True, privacy_acknowledged_at=datetime.now(UTC))


def test_gemini_payload_is_structured_toolless_and_minimized(verified_claim) -> None:
    opportunity = make_opportunity(ai_processing_allowed=True)
    models = _GeminiModels(
        _GeminiResponse(_analysis(opportunity.record_id, verified_claim.claim_id).model_dump_json())
    )
    analyzer = GeminiAnalyzer(
        "synthetic-key",
        _gemini_config(),
        MemoryAnalysisQuotaLedger(),
        client=_GeminiClient(models),
    )
    response = analyzer.analyze(opportunity, [verified_claim])
    assert response.provider == "gemini"
    assert response.input_tokens == 321
    assert models.observed["model"] == "gemini-3.7-flash"
    config = models.observed["config"]
    assert config.tools is None
    assert config.response_mime_type == "application/json"
    assert config.thinking_config.thinking_level.value == "LOW"
    prompt = models.observed["contents"]
    assert "synthetic-key" not in prompt
    assert "@" not in prompt
    assert "https://" not in prompt


class _ProviderError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"provider status {status_code}")
        self.status_code = status_code


@pytest.mark.parametrize("status", [400, 401, 403])
def test_gemini_config_and_auth_errors_never_fallback(status, verified_claim) -> None:
    opportunity = make_opportunity(ai_processing_allowed=True)
    primary = GeminiAnalyzer(
        "synthetic-key",
        _gemini_config(),
        MemoryAnalysisQuotaLedger(),
        client=_GeminiClient(_GeminiModels(error=_ProviderError(status))),
    )
    fallback = _Provider(
        AnalysisResponse(
            _analysis(opportunity.record_id, verified_claim.claim_id), "ollama", "fake"
        )
    )
    with pytest.raises(ProviderRequestError):
        AnalysisOrchestrator(primary, fallback).analyze(opportunity, [verified_claim])


@pytest.mark.parametrize("status", [429, 502, 503, 504])
def test_gemini_transient_and_quota_errors_may_fallback(status, verified_claim) -> None:
    opportunity = make_opportunity(ai_processing_allowed=True)
    primary = GeminiAnalyzer(
        "synthetic-key",
        _gemini_config(),
        MemoryAnalysisQuotaLedger(),
        client=_GeminiClient(_GeminiModels(error=_ProviderError(status))),
    )
    fallback = _Provider(
        AnalysisResponse(
            _analysis(opportunity.record_id, verified_claim.claim_id), "ollama", "fake"
        )
    )
    response = AnalysisOrchestrator(primary, fallback).analyze(opportunity, [verified_claim])
    assert response.provider == "ollama"
    assert f"HTTP {status}" in response.fallback_reason


class _LabeledEvaluationProvider:
    provider_id = "evaluation"
    model_id = "synthetic"
    max_rows_per_run = 20

    def analyze(self, opportunity, verified_claims):
        case_number = int(opportunity.source_record.source_record_id.rsplit("-", 1)[1])
        positive = case_number <= 10
        claim_id = verified_claims[0].claim_id
        analysis = AIAnalysis(
            record_id=opportunity.record_id,
            required_skill_score=35 if positive else 0,
            evidence_relevance_score=25 if positive else 0,
            scope_seniority_score=15 if positive else 0,
            matched_evidence_ids=[claim_id] if positive else [],
            rationale="Synthetic labeled evaluation output.",
            confidence=1,
            draft=None,
        )
        return AnalysisResponse(analysis, self.provider_id, self.model_id)

    def close(self) -> None:
        pass


def test_ollama_evaluation_harness_requires_and_scores_twenty_cases() -> None:
    fixtures = load_evaluation_fixtures(Path("tests/fixtures/ollama_eval.json"))
    report = evaluate_provider(_LabeledEvaluationProvider(), fixtures)
    assert report.total == 20
    assert report.completed == 20
    assert report.label_matches == 20
    assert report.passed
