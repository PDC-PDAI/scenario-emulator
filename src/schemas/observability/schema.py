from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class NodeLocus(str, Enum):
    JOB = "JOB"
    COORDINATOR = "COORDINATOR"
    SUB_GER = "SUB-GER"
    MCP = "MCP"
    SYSTEM = "SYSTEM"
    RESPONSE_GENERATOR = "ATTACK-GEN"
    EVALUATOR = "SUB-EVAL"
    ORACLE = "ORACLE"


class TraceNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    locus: NodeLocus
    depends_on: list[str] = Field(default_factory=list)
    name: str
