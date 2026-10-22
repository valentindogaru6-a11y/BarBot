import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.models.assistant import AssistantIntentName
from app.models.assistant_planner import PlannerCapability, PlannerContextSnapshot, PlannerDecision
from app.services.assistant.orchestrator import AssistantOrchestrator
from app.services.assistant.providers import GeminiIntentClient, HttpResponse, ProviderError, ProviderErrorKind


def decision(**overrides):
    data = {"dialogue_act":"new_command","domain":"catalog","capability_id":"catalog.search","target_entity_id":None,"arguments":{"query":"облепиха"},"confidence":9000,"needs_clarification":False,"clarification_question":None,"read_only":True,"mutation":False,"context_updates":{}}
    data.update(overrides)
    return PlannerDecision.model_validate(data)


def snapshot(**overrides):
    data = {"actor_role":"super_admin","available_capability_ids":("catalog.search","catalog.manage"),"last_successful_entity_id":"oblepikha","last_successful_entity_name":"Облепиха"}
    data.update(overrides)
    return PlannerContextSnapshot.model_validate(data)


CAPS = (
    PlannerCapability(capability_id="catalog.search",domain="catalog",read_only=True,mutation=False,required_arguments=("query",)),
    PlannerCapability(capability_id="catalog.manage",domain="catalog",read_only=False,mutation=True),
)


class FakePlanner:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result or decision(), error, []
    async def plan(self, text, *, snapshot, capabilities):
        self.calls.append((text, snapshot, capabilities))
        if self.error: raise self.error
        return self.result


class NeverIntent:
    async def classify(self, *args, **kwargs):
        raise AssertionError("legacy classifier must not be called")


def test_planner_config_defaults_and_rejects_unknown_mode():
    assert Settings(_env_file=None, bot_token="test").ai_planner_mode == "local_first"
    with pytest.raises(ValidationError): Settings(_env_file=None, bot_token="test", ai_planner_mode="unsafe")


def test_decision_is_strict_and_has_no_authority_fields():
    with pytest.raises(ValidationError): PlannerDecision.model_validate({**decision().model_dump(), "confirmed":True})
    with pytest.raises(ValidationError): PlannerDecision.model_validate({**decision().model_dump(), "role":"owner"})


def test_ai_first_uses_planner_and_maps_read_only_catalog():
    planner = FakePlanner()
    result = asyncio.run(AssistantOrchestrator(NeverIntent(),planner_mode="ai_first",planner_client=planner).interpret("свободная фраза",planner_snapshot=snapshot(),planner_capabilities=CAPS))
    assert result.intent is AssistantIntentName.FIND_TECH_CARD
    assert result.query == "облепиха"
    assert len(planner.calls) == 1


def test_shadow_never_replaces_local_result():
    planner = FakePlanner(decision(capability_id="catalog.manage",read_only=False,mutation=True,needs_clarification=True,clarification_question="Что изменить?",dialogue_act="follow_up"))
    result = asyncio.run(AssistantOrchestrator(NeverIntent(),planner_mode="shadow",planner_client=planner).interpret("покажи техкарту Kiss Me",planner_snapshot=snapshot(),planner_capabilities=CAPS))
    assert result.intent is AssistantIntentName.FIND_TECH_CARD
    assert len(planner.calls) == 1


@pytest.mark.parametrize("act,text", [("follow_up","измени состав"),("correction","нет, я имел в виду изменить состав облепиховой пфки")])
def test_seabuckthorn_followup_and_correction_keep_stable_id(act, text):
    planned = decision(dialogue_act=act,capability_id="catalog.manage",target_entity_id="oblepikha",arguments={},read_only=False,mutation=True,needs_clarification=True,clarification_question="Что именно изменить в составе «Облепихи»?")
    planner = FakePlanner(planned)
    result = asyncio.run(AssistantOrchestrator(NeverIntent(),planner_mode="ai_first",planner_client=planner).interpret(text,planner_snapshot=snapshot(),planner_capabilities=CAPS))
    assert result.intent is AssistantIntentName.CLARIFY
    assert "Облепихи" in result.clarification
    assert planner.calls[0][1].last_successful_entity_id == "oblepikha"


def test_ai_first_degrades_safely_with_context():
    planner = FakePlanner(error=RuntimeError("offline"))
    result = asyncio.run(AssistantOrchestrator(NeverIntent(),planner_mode="ai_first",planner_client=planner).interpret("измени состав",planner_snapshot=snapshot(),planner_capabilities=CAPS))
    assert result.intent is AssistantIntentName.CLARIFY
    assert "что именно" in result.clarification.casefold()


def test_planner_cancellation_propagates_and_next_request_is_allowed():
    class BlockingPlanner(FakePlanner):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
        async def plan(self, text, *, snapshot, capabilities):
            self.started.set()
            await asyncio.Event().wait()

    async def scenario():
        blocking = BlockingPlanner()
        orchestrator = AssistantOrchestrator(
            NeverIntent(), planner_mode="ai_first", planner_client=blocking
        )
        task = asyncio.create_task(orchestrator.interpret(
            "запрос", planner_snapshot=snapshot(), planner_capabilities=CAPS
        ))
        await blocking.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        orchestrator.planner_client = FakePlanner()
        return await orchestrator.interpret(
            "новый запрос", planner_snapshot=snapshot(), planner_capabilities=CAPS
        )

    assert asyncio.run(scenario()).intent is AssistantIntentName.FIND_TECH_CARD


def test_planner_warehouse_action_still_requires_local_evidence():
    cap = PlannerCapability(capability_id="warehouse.prepare_writeoff",domain="warehouse",read_only=False,mutation=True)
    planner = FakePlanner(decision(domain="warehouse",capability_id=cap.capability_id,arguments={"product":"апельсин","quantity":"0.2","unit":"кг"},read_only=False,mutation=True))
    result = asyncio.run(AssistantOrchestrator(NeverIntent(),planner_mode="ai_first",planner_client=planner).interpret("можно ли списать апельсин",planner_snapshot=snapshot(available_capability_ids=(cap.capability_id,)),planner_capabilities=(cap,)))
    assert result.intent is AssistantIntentName.CLARIFY


class FakeTransport:
    def __init__(self, payload):
        body = {"candidates":[{"content":{"parts":[{"text":json.dumps(payload)}]}}]}
        self.response, self.calls = HttpResponse(200,json.dumps(body).encode()), []
    async def post_json(self, url, **kwargs):
        self.calls.append((url,kwargs)); return self.response


def test_provider_uses_strict_schema_and_rejects_invented_id():
    transport = FakeTransport(decision(target_entity_id="invented").model_dump(mode="json"))
    client = GeminiIntentClient(transport,api_key="secret",model="flash",timeout_seconds=5)
    with pytest.raises(ProviderError) as caught: asyncio.run(client.plan("request",snapshot=snapshot(),capabilities=CAPS))
    assert caught.value.kind is ProviderErrorKind.MALFORMED_RESPONSE
    _, call = transport.calls[0]
    assert call["payload"]["generationConfig"]["responseJsonSchema"]["additionalProperties"] is False
    assert "secret" not in json.dumps(call["payload"])


def test_versioned_corpus_has_every_required_group():
    payload = json.loads((Path(__file__).parent/"fixtures/assistant_planner_corpus_v1.json").read_text())
    groups = {case["group"] for case in payload["cases"]}
    assert payload["version"] == "bar-jarvis-planner-v1"
    assert {"corrections","pronoun_followups","topic_switch","topic_return","incomplete_mutations","colloquial_catalog","mixed_ru_ua_typos","attendance","reports_delivery","administration","false_positive_mutations","confirmation_cancellation","provider_failures"} <= groups
    assert len(payload["cases"]) == 17


def test_corpus_records_reproducible_local_baseline():
    mapping = {
        AssistantIntentName.FIND_TECH_CARD: "catalog.search",
        AssistantIntentName.PREPARE_WRITEOFF: "warehouse.prepare_writeoff",
        AssistantIntentName.PREPARE_TRANSFER: "warehouse.prepare_transfer",
        AssistantIntentName.LIST_OPEN_ATTENDANCE_SESSIONS: "attendance.list_open",
        AssistantIntentName.GET_CURRENT_SHIFT_SUMMARY: "warehouse.shift_summary",
    }
    payload = json.loads((Path(__file__).parent/"fixtures/assistant_planner_corpus_v1.json").read_text())
    actual = []
    for case in payload["cases"]:
        local = AssistantOrchestrator.local_intent(case["text"])
        actual.append((mapping.get(local.intent) if local else None) == case["capability_id"])
    assert actual.count(True) == 7
    assert actual.count(False) == 10
