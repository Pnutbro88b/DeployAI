#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DeployAI — Orchestration and deployment assistant for Loopa yield aggregation vaults.
Manages configs, strategy catalogs, simulations, and deployment plans for best DeFi rates across crypto.
Usage:
  python DeployAI.py [--config PATH] [command] [options]
  python DeployAI.py interactive
  python DeployAI.py snapshot
  python DeployAI.py simulate --vault loopa-usdc --days 365 --deposit 100000
  python DeployAI.py plan --vault loopa-usdc
  python DeployAI.py load --file config.json
  python DeployAI.py save --file out.json
  python DeployAI.py strategies | vaults | chains | version | demo
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

APP_NAME = "DeployAI"
DEPLOYAI_VERSION = "1.0.0"
LOOPA_ENGINE = "Loopa"
CONFIG_DIR = ".deployai"
CONFIG_FILE = "config.json"
DEFAULT_CONFIG_PATH = os.environ.get("DEPLOYAI_CONFIG", "")

# -----------------------------------------------------------------------------
# Domain models
# -----------------------------------------------------------------------------


@dataclass
class Chain:
    name: str
    rpc: str
    block_time_s: float = 12.0
    base_gas_price_gwei: float = 2.0

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "rpc": self.rpc,
            "block_time_s": self.block_time_s,
            "base_gas_price_gwei": self.base_gas_price_gwei,
        }


@dataclass
class Protocol:
    name: str
    chain: str
    kind: str  # lending, dex, staking

    def as_dict(self) -> dict:
        return {"name": self.name, "chain": self.chain, "kind": self.kind}


@dataclass
class StrategyConfig:
    id: str
    name: str
    asset: str
    chain: str
    protocol: str
    risk_band: str
    base_apr: float
    boost_apr: float
    performance_fee: float
    max_capacity: float
    metadata: Dict[str, str] = field(default_factory=dict)

    def gross_apr(self) -> float:
        return self.base_apr + self.boost_apr

    def net_apr(self) -> float:
        return self.gross_apr() * (1.0 - self.performance_fee)

    def as_dict(self) -> dict:
        d = {
            "id": self.id,
            "name": self.name,
            "asset": self.asset,
            "chain": self.chain,
            "protocol": self.protocol,
            "risk_band": self.risk_band,
            "base_apr": self.base_apr,
            "boost_apr": self.boost_apr,
            "performance_fee": self.performance_fee,
            "max_capacity": self.max_capacity,
            "gross_apr": self.gross_apr(),
            "net_apr": self.net_apr(),
            "metadata": self.metadata,
        }
        return d


@dataclass
class VaultConfig:
    id: str
    name: str
    asset: str
    base_chain: str
    management_fee: float
    withdrawal_fee: float
    default_risk_band: str
    rebalance_interval_s: int
    strategies: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "asset": self.asset,
