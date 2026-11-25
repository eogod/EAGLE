"""Module for export chain in JSON / YAML by file stream
with LLM summarization of whole chain"""

import datetime
import io
import json
from typing import Any

import yaml
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api import deps
from app.core.llm_templ import LLMTemplates
from app.models import AttackChain, AttackStep, User
from app.services.llm_service import llm_service

router = APIRouter()

# TODO: add POST /import-chain that reads this export format back into planned steps.


def result_to_dict_recursive(
    instance: Any, visited: set | None = None
) -> dict[str, Any]:
    """format SQLalchemy result to JSON like python dict"""
    # code based on https://www.geeksforgeeks.org/python
    #               /serialize-python-sqlalchemy-result-to-json/
    # maybe rust / c / go in .so can do it faster
    # but chain is not so big, with enough memory this works
    if visited is None:
        visited = set()

    if not instance:
        return {}
    # because id has all tables
    instance_id = (instance.__class__, getattr(instance, "id", None))
    if instance_id in visited:
        return {"id": getattr(instance, "id", None), "__ref__": "Circular"}
    visited.add(instance_id)

    ins = inspect(instance)
    data = {}

    if ins is None:
        raise ValueError("to dict instance is None")

    for column in ins.mapper.column_attrs:
        value = getattr(instance, column.key)
        # convert date/time to iso string format
        if isinstance(value, datetime.datetime | datetime.date):
            data[column.key] = value.isoformat()
        else:
            data[column.key] = value

    for rel in ins.mapper.relationships:
        # skip unloaded relationships from options selectinload
        if rel.key not in ins.unloaded:
            related_obj = getattr(instance, rel.key)
            # process related objects
            if related_obj is not None:
                if rel.uselist:
                    # try to sort by id dict attr
                    related_list = list(related_obj)
                    related_list.sort(key=lambda x: getattr(x, "id", None))
                    # recursive as list Result from generator
                    data[rel.key] = [
                        result_to_dict_recursive(child, visited.copy())
                        for child in related_list
                    ]
                else:
                    data[rel.key] = result_to_dict_recursive(
                        related_obj, visited.copy()
                    )  # otherwise skip and return data
    return data


async def prepare_export_data(chain_id: int, session: AsyncSession) -> dict[str, Any]:
    """get from db, format to dict"""
    result = await session.execute(
        select(AttackChain)
        .where(AttackChain.id == chain_id)
        .options(  # TEST ME PLS
            selectinload(AttackChain.user),  # get also user
            selectinload(AttackChain.current_phase),  # get also last phase
            selectinload(  # get also AttackStep as result list
                AttackChain.attack_step
            ).selectinload(AttackStep.agent),  # get also Agent
        )
    )
    c_chain: AttackChain = result.scalars().first()
    data_dict = result_to_dict_recursive(c_chain)

    return data_dict


@router.get("/json/{chain_id}")
async def export_json(
    chain_id: int,
    is_with_llm: bool = False,
    session: AsyncSession = Depends(deps.get_session),
    current_user: User = Depends(deps.get_current_user),
) -> StreamingResponse:
    """export chain to JSON"""
    data: dict = await prepare_export_data(chain_id, session)
    chain_ca: list[AttackChain] = await session.execute(
        select(AttackChain).where(AttackChain.id == chain_id)
    )
    # get first object of select
    chain_c: AttackChain = chain_ca.scalars().first()
    chain_name: str = chain_c.chain_name
    if is_with_llm:
        data_s = json.dumps(data, ensure_ascii=False, indent=4)
        # TODO: add filter for llm in
        llm_a: str = await llm_service.query_llm(
            LLMTemplates.CHAIN_SUMMARIZATION.format(data=data_s)
        )
        data_e = {"chain_name": chain_name, "chain": data, "llm": llm_a}
    else:
        data_e = {"chain_name": chain_name, "chain": data, "llm": "28"}
    json_str = json.dumps(data_e, ensure_ascii=False, indent=2)
    json_bytes = json_str.encode("utf-8")

    file_like = io.BytesIO(json_bytes)

    return StreamingResponse(
        file_like,
        media_type="application/json",
        headers={  # as file for download
            "Content-Disposition": f"attachment; filename=export_{chain_name}.json"
        },
    )


@router.get("/yaml/{chain_id}")
async def export_yaml(
    chain_id: int,
    is_with_llm: bool = False,
    session: AsyncSession = Depends(deps.get_session),
    current_user: User = Depends(deps.get_current_user),
) -> StreamingResponse:
    """export chain to YAML"""
    data = await prepare_export_data(chain_id, session)
    chain_ca: list[AttackChain] = await session.execute(
        select(AttackChain).where(AttackChain.id == chain_id)
    )
    # get first object of select
    chain_c: AttackChain = chain_ca.scalars().first()
    chain_name: str = chain_c.chain_name
    if is_with_llm:
        data_s = json.dumps(data, ensure_ascii=False, indent=4)
        llm_a: str = await llm_service.query_llm(
            LLMTemplates.CHAIN_SUMMARIZATION.format(data=data_s)
        )
        data_e = {"chain_name": chain_name, "chain": data, "llm": llm_a}
    else:
        data_e = {"chain_name": chain_name, "chain": data, "llm": "28"}
    yaml_str = yaml.dump(data_e, allow_unicode=True, default_flow_style=False, indent=2)
    yaml_bytes = yaml_str.encode("utf-8")

    file_like = io.BytesIO(yaml_bytes)
    return StreamingResponse(
        file_like,
        media_type="application/x-yaml",
        headers={  # as file for download
            "Content-Disposition": f"attachment; filename=export_{chain_name}.yaml"
        },
    )


# TODO: export a run manifest too: run id, timings, artifacts and assertion results.
