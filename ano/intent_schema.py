"""Vendor-neutral intent schema. The LLM must fill THIS, never raw CLI."""
from __future__ import annotations
from ipaddress import IPv4Network
from typing import Literal, Optional
from pydantic import BaseModel, Field


class Traffic(BaseModel):
    src: IPv4Network
    dst: IPv4Network
    protocol: Literal["ip", "tcp", "udp", "icmp"]
    dst_port: Optional[int] = Field(None, ge=1, le=65535)


class Intent(BaseModel):
    """First supported intent family: traffic policy. New families (VLAN, routing, NAT...) are new classes."""
    kind: Literal["block_traffic", "permit_traffic"]
    name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,40}$", description="short id, e.g. block-http-lan1-lan3")
    match: Traffic
    must_still_work: list[Traffic] = Field(default_factory=list,
                                           description="traffic the user said must keep working")
    unclear: list[str] = Field(default_factory=list,
                               description="anything the prompt did not state; becomes a question, never a guess")
