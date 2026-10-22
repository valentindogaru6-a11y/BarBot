#!/usr/bin/env python3
"""Explicitly gated live planner diagnostic; never executes BarBot tools."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path

from app.config import Settings
from app.models.assistant import AssistantDomain
from app.models.assistant_planner import PlannerCapability, PlannerContextSnapshot
from app.services.assistant import (
    AioHttpTransport,
    AssistantCatalogRetriever,
    GeminiPlannerProvider,
    GroqPlannerProvider,
    MultiProviderPlanner,
    PlannerSmokeCase,
    PlannerSmokeHarness,
    ProviderError,
    candidate_key,
)
from app.storage.json_storage import JsonCatalogStorage

PROJECT_ROOT = Path(__file__).resolve().parents[1]


async def _run() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--live-authorized",
        action="store_true",
        help="acknowledge a one-time owner authorization for authenticated calls",
    )
    parser.add_argument("--provider", choices=("groq", "gemini"), required=True)
    args = parser.parse_args()
    if not args.live_authorized:
        parser.error("live calls are disabled without --live-authorized")

    settings = Settings()
    catalog = JsonCatalogStorage(PROJECT_ROOT / "data" / "catalog.json").load()
    retrieval = AssistantCatalogRetriever(catalog).retrieve("Облепиха")
    if len(retrieval.exact) != 1:
        raise SystemExit("tracked catalog target is not unique")
    target_id = candidate_key(retrieval.exact[0])

    capabilities = (
        PlannerCapability(
            capability_id="catalog.search", domain="catalog",
            read_only=True, mutation=False,
            required_arguments=("query",), allowed_arguments=("query",),
        ),
        PlannerCapability(
            capability_id="catalog.get", domain="catalog",
            read_only=True, mutation=False,
            required_arguments=("entity_ref",),
            allowed_arguments=(),
        ),
        PlannerCapability(
            capability_id="catalog.manage", domain="catalog",
            read_only=False, mutation=True,
            allowed_arguments=("reason",),
        ),
        PlannerCapability(
            capability_id="capabilities.list", domain="general",
            read_only=True, mutation=False,
            required_arguments=("query",), allowed_arguments=("query",),
        ),
    )
    snapshot = PlannerContextSnapshot(
        actor_role="super_admin",
        available_capability_ids=tuple(item.capability_id for item in capabilities),
        current_domain=AssistantDomain.CATALOG,
        candidate_entity_ids=(target_id,),
    )
    transport = AioHttpTransport()
    if args.provider == "groq":
        if settings.groq_api_key is None:
            raise SystemExit("Groq key is not configured")
        provider = GroqPlannerProvider(
            transport,
            api_key=settings.groq_api_key.get_secret_value(),
            model=settings.groq_planner_model,
        )
    else:
        if settings.gemini_api_key is None:
            raise SystemExit("Gemini key is not configured")
        provider = GeminiPlannerProvider(
            transport,
            api_key=settings.gemini_api_key.get_secret_value(),
            model=settings.gemini_fast_model,
        )
    client = MultiProviderPlanner(provider, mode="local_first")
    harness = PlannerSmokeHarness(client, live_authorized=True, max_calls=4)
    cases = (
        PlannerSmokeCase("case_1", "найди пф для облепихи"),
        PlannerSmokeCase("case_2", "измени состав"),
        PlannerSmokeCase(
            "case_3", "нет, я имел в виду изменить состав облепиховой пфки"
        ),
        PlannerSmokeCase(
            "case_4", "добавь туда 100 мл апельсинового фреша"
        ),
    )
    failure_category = None
    for index, case in enumerate(cases):
        try:
            decision = await harness.run_case(
                case,
                snapshot=snapshot,
                capabilities=capabilities,
                expected_target_id=target_id,
            )
        except ProviderError as exc:
            failure_category = exc.kind.value
            break
        except Exception:
            failure_category = "internal"
            break
        # Emit each bounded validated result immediately; no request/response
        # body, stable ID, key, header or synthetic actor identifier is printed.
        print(json.dumps(asdict(harness.results[-1]), separators=(",", ":")), flush=True)
        if index == 0:
            snapshot = harness.update_selected_entity(
                snapshot, stable_id=target_id, display_name="Облепиха"
            )
        elif decision.context_updates.current_domain is not None:
            snapshot = snapshot.model_copy(
                update={"current_domain": decision.context_updates.current_domain}
            )
    # The bounded final report is reconstructed from retained validated rows;
    # a later failed case cannot erase an earlier result.
    print(
        json.dumps(
            {
                "attempted_calls": harness.attempted_calls,
                "completed_calls": harness.completed_calls,
                "failure_category": failure_category,
                "results": [asdict(result) for result in harness.results],
            },
            separators=(",", ":"),
        ),
        flush=True,
    )
    return 0 if failure_category is None else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
