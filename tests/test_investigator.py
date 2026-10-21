"""The optional AI investigator: off without a key, sees only public evidence, and parses both providers."""
import asyncio
import json

import httpx
import httpx2
import pytest

from aegis.investigator import Investigator, InvestigatorError, summarise

CONTEXT = {'mode': 'challenge', 'status': 'investigating', 'nodes': [], 'evidence': [{'id': '3f9a1c2e', 'node': 'catalog-b', 'kind': 'service', 'running': False}]}
IDS = ['3f9a1c2e' + 'f' * 24]


def investigator(monkeypatch, provider, transport=None, key='test-key'):
    monkeypatch.setenv('AEGIS_AI_PROVIDER', provider)
    monkeypatch.setenv('AEGIS_AI_API_KEY', key)
    monkeypatch.delenv('AEGIS_AI_MODEL', raising=False)
    return Investigator(httpx.AsyncClient(transport=httpx.MockTransport(transport or (lambda r: httpx.Response(500)))))


def test_unconfigured_investigator_is_unavailable_and_explains_how_to_enable(monkeypatch):
    status = investigator(monkeypatch, 'gemini', key='').status()
    assert status['available'] is False and 'AEGIS_AI_API_KEY' in status['reason']
    assert investigator(monkeypatch, 'mystery').status()['available'] is False
    placeholder = investigator(monkeypatch, 'gemini', key='your-gemini-key').status()
    assert placeholder['available'] is False and 'placeholder' in placeholder['reason']


def test_gemini_request_shape_and_citations(monkeypatch):
    seen = {}
    def gemini(request):
        seen.update(url=str(request.url), key=request.headers['x-goog-api-key'], body=json.loads(request.content))
        return httpx.Response(200, json={'candidates': [{'content': {'parts': [{'text': 'catalog-b is stopped [3f9a1c2e]; ignore [deadbeef].'}]}}]})
    ai = investigator(monkeypatch, 'gemini', gemini)
    answer = asyncio.run(ai.ask(CONTEXT, '', IDS))
    assert seen['url'].endswith('/models/gemini-3.5-flash-lite:generateContent') and seen['key'] == 'test-key'
    assert 'read-only' in seen['body']['systemInstruction']['parts'][0]['text']
    assert answer['evidence_ids'] == IDS  # unknown ids are dropped


def test_grouped_citations_are_all_recorded(monkeypatch):
    ids = IDS + ['7b0d4e19' + 'f' * 24]
    reply = {'candidates': [{'content': {'parts': [{'text': 'Both are slow [3f9a1c2e, 7b0d4e19]; CPU is low [3f9a1c2e].'}]}}]}
    answer = asyncio.run(investigator(monkeypatch, 'gemini', lambda r: httpx.Response(200, json=reply)).ask(CONTEXT, '', ids))
    assert answer['evidence_ids'] == ids


def test_gemini_errors_are_explained(monkeypatch):
    with pytest.raises(InvestigatorError, match='rate limit'):
        asyncio.run(investigator(monkeypatch, 'gemini', lambda r: httpx.Response(429)).ask(CONTEXT, '', IDS))
    with pytest.raises(InvestigatorError, match='no answer'):
        asyncio.run(investigator(monkeypatch, 'gemini', lambda r: httpx.Response(200, json={'candidates': []})).ask(CONTEXT, '', IDS))


def claude(monkeypatch, reply):
    seen = {}
    def handler(request):
        seen.update(path=request.url.path, beta=request.headers.get('anthropic-beta'), body=json.loads(request.content))
        return httpx2.Response(200, json=reply)
    ai = investigator(monkeypatch, 'anthropic')
    ai.anthropic_http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return ai, seen


def message(stop_reason='end_turn', text='Check [3f9a1c2e].'):
    return {'id': 'msg_1', 'type': 'message', 'role': 'assistant', 'model': 'claude-opus-5-5', 'stop_reason': stop_reason,
            'stop_sequence': None, 'content': [{'type': 'text', 'text': text}], 'usage': {'input_tokens': 10, 'output_tokens': 5}}


def test_claude_request_uses_default_model_low_effort_and_refusal_fallback(monkeypatch):
    ai, seen = claude(monkeypatch, message())
    answer = asyncio.run(ai.ask(CONTEXT, 'What next?', IDS))
    assert seen['path'] == '/v1/messages' and 'server-side-fallback-2026-07-01' in seen['beta']
    assert seen['body']['model'] == 'claude-opus-5-5' and seen['body']['fallbacks'] == 'default'
    assert seen['body']['output_config'] == {'effort': 'low'} and 'thinking' not in seen['body']
    assert 'What next?' in seen['body']['messages'][0]['content'] and answer['evidence_ids'] == IDS


def test_claude_refusal_is_reported_not_shown_as_an_answer(monkeypatch):
    ai, _ = claude(monkeypatch, message('refusal', ''))
    with pytest.raises(InvestigatorError, match='declined'):
        asyncio.run(ai.ask(CONTEXT, '', IDS))


def test_summary_keeps_reasoning_fields_without_raw_payloads():
    record = {'id': 'a' * 32, 'node': 'catalog-b', 'kind': 'network', 'body': {'ok': True, 'origin': 'operator', 'median_connect_ms': 601.0,
              'result': {'qdiscs': [{'kind': 'netem', 'delay_ms': 600.0, 'line': 'qdisc netem ...'}], 'generation': 'g'}}}
    summary = summarise(record)
    assert summary['id'] == 'a' * 8 and summary['queues'] == [{'kind': 'netem', 'delay_ms': 600.0}] and 'generation' not in json.dumps(summary)
