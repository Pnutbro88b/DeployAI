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
            "base_chain": self.base_chain,
            "management_fee": self.management_fee,
            "withdrawal_fee": self.withdrawal_fee,
            "default_risk_band": self.default_risk_band,
            "rebalance_interval_s": self.rebalance_interval_s,
            "strategies": list(self.strategies),
        }


@dataclass
class DeploymentPlan:
    vault_id: str
    version: str
    created_at: int
    steps: List[Dict[str, Any]] = field(default_factory=list)

    def add_step(self, kind: str, description: str, **extra: Any) -> None:
        entry: Dict[str, Any] = {"kind": kind, "description": description}
        entry.update(extra)
        self.steps.append(entry)

    def as_dict(self) -> dict:
        return {
            "vault_id": self.vault_id,
            "version": self.version,
            "created_at": self.created_at,
            "steps": list(self.steps),
        }


@dataclass
class SimulationResult:
    vault_id: str
    start_ts: int
    end_ts: int
    initial_deposit: float
    final_value: float
    steps: List[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "vault_id": self.vault_id,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "initial_deposit": self.initial_deposit,
            "final_value": self.final_value,
            "steps": list(self.steps),
        }


# -----------------------------------------------------------------------------
# Registry
# -----------------------------------------------------------------------------


class Registry:
    def __init__(self) -> None:
        self.chains: Dict[str, Chain] = {}
        self.protocols: Dict[str, Protocol] = {}
        self.strategies: Dict[str, StrategyConfig] = {}
        self.vaults: Dict[str, VaultConfig] = {}

    def add_chain(self, chain: Chain) -> None:
        if chain.name in self.chains:
            raise ValueError(f"Chain already exists: {chain.name}")
        self.chains[chain.name] = chain

    def get_chain(self, name: str) -> Optional[Chain]:
        return self.chains.get(name)

    def add_protocol(self, proto: Protocol) -> None:
        key = f"{proto.chain}:{proto.name}"
        if key in self.protocols:
            raise ValueError(f"Protocol already exists: {key}")
        if proto.chain not in self.chains:
            raise ValueError(f"Unknown chain for protocol: {proto.chain}")
        self.protocols[key] = proto

    def get_protocol(self, chain: str, name: str) -> Optional[Protocol]:
        return self.protocols.get(f"{chain}:{name}")

    def add_strategy(self, strat: StrategyConfig) -> None:
        if strat.id in self.strategies:
            raise ValueError(f"Strategy already exists: {strat.id}")
        key = f"{strat.chain}:{strat.protocol}"
        if key not in self.protocols:
            raise ValueError(f"Unknown protocol {strat.protocol} on chain {strat.chain}")
        self.strategies[strat.id] = strat

    def get_strategy(self, strat_id: str) -> Optional[StrategyConfig]:
        return self.strategies.get(strat_id)

    def add_vault(self, vault: VaultConfig) -> None:
        if vault.id in self.vaults:
            raise ValueError(f"Vault already exists: {vault.id}")
        if vault.base_chain not in self.chains:
            raise ValueError(f"Unknown base chain: {vault.base_chain}")
        self.vaults[vault.id] = vault

    def get_vault(self, vault_id: str) -> Optional[VaultConfig]:
        return self.vaults.get(vault_id)

    def load_from_file(self, path: str) -> None:
        with open(path, "r", encoding="utf8") as f:
            data = json.load(f)
        for ch in data.get("chains", []):
            self.add_chain(Chain(**ch))
        for pr in data.get("protocols", []):
            self.add_protocol(Protocol(**pr))
        for st in data.get("strategies", []):
            self.add_strategy(StrategyConfig(**{k: v for k, v in st.items() if k != "gross_apr" and k != "net_apr"}))
        for vt in data.get("vaults", []):
            self.add_vault(VaultConfig(**vt))

    def snapshot(self) -> dict:
        return {
            "chains": [c.as_dict() for c in self.chains.values()],
            "protocols": [p.as_dict() for p in self.protocols.values()],
            "strategies": [s.as_dict() for s in self.strategies.values()],
            "vaults": [v.as_dict() for v in self.vaults.values()],
        }


# -----------------------------------------------------------------------------
# Simulator
# -----------------------------------------------------------------------------


class Simulator:
    def __init__(self, registry: Registry) -> None:
        self.registry = registry

    def simulate_vault(
        self,
        vault_id: str,
        initial_deposit: float,
        days: int,
        rebalance_every_days: int = 7,
        noise_std: float = 0.02,
    ) -> SimulationResult:
        vault = self.registry.get_vault(vault_id)
        if not vault:
            raise ValueError(f"Unknown vault: {vault_id}")
        if not vault.strategies:
            raise ValueError("Vault has no strategies configured")

        now = int(time.time())
        end_ts = now + days * 86400
        value = float(initial_deposit)
        steps: List[dict] = []

        weights = self._compute_weights(vault)
        for day in range(days):
            daily_yield = 0.0
            for sid, weight in weights.items():
                strat = self.registry.get_strategy(sid)
                if not strat:
                    continue
                apr = strat.net_apr()
                noisy_apr = self._apply_noise(apr, noise_std)
                daily_rate = noisy_apr / 365.0
                alloc = value * weight
                daily_yield += alloc * daily_rate
            value += daily_yield
            if (day + 1) % rebalance_every_days == 0:
                weights = self._compute_weights(vault)
            if (day + 1) % 30 == 0 or day == days - 1:
                steps.append({"day": day + 1, "timestamp": now + (day + 1) * 86400, "value": value})

        return SimulationResult(
            vault_id=vault_id,
            start_ts=now,
            end_ts=end_ts,
            initial_deposit=initial_deposit,
            final_value=value,
            steps=steps,
        )

    def _compute_weights(self, vault: VaultConfig) -> Dict[str, float]:
        scores: Dict[str, float] = {}
        for sid in vault.strategies:
            strat = self.registry.get_strategy(sid)
            if not strat:
                continue
            if strat.risk_band != vault.default_risk_band:
                continue
            score = max(strat.net_apr(), 0.0)
            scores[sid] = score
        if not scores:
            eq = 1.0 / max(len(vault.strategies), 1)
            return {sid: eq for sid in vault.strategies}
        total = sum(scores.values())
        return {sid: s / total for sid, s in scores.items()}

    @staticmethod
    def _apply_noise(apr: float, std: float) -> float:
        if std <= 0:
            return apr
        return max(0.0, min(1.0, apr + random.gauss(0.0, std)))


# -----------------------------------------------------------------------------
# Planner
# -----------------------------------------------------------------------------


class Planner:
    def __init__(self, registry: Registry) -> None:
        self.registry = registry

    def build_plan(self, vault_id: str, version: str = "v1") -> DeploymentPlan:
        vault = self.registry.get_vault(vault_id)
        if not vault:
            raise ValueError(f"Unknown vault: {vault_id}")
        plan = DeploymentPlan(vault_id=vault_id, version=version, created_at=int(time.time()))

        plan.add_step(
            "deploy_vault",
            f"Deploy Loopa vault {vault.name} on {vault.base_chain}",
            asset=vault.asset,
            management_fee=str(vault.management_fee),
            withdrawal_fee=str(vault.withdrawal_fee),
        )
        for sid in vault.strategies:
            strat = self.registry.get_strategy(sid)
            if not strat:
                continue
            plan.add_step(
                "register_strategy",
                f"Register strategy {strat.name} on {strat.chain}/{strat.protocol}",
                asset=strat.asset,
                risk=strat.risk_band,
                base_apr=str(strat.base_apr),
                boost_apr=str(strat.boost_apr),
            )
        plan.add_step(
            "set_rebalance",
            f"Set rebalance interval to {vault.rebalance_interval_s} seconds",
            interval_s=str(vault.rebalance_interval_s),
        )
        return plan


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def pretty_json(obj: Any) -> str:
    return json.dumps(obj, indent=2, sort_keys=True)


def fmt_pct(v: float) -> str:
    return f"{v * 100:.2f}%"


def fmt_num(v: float) -> str:
    if abs(v) >= 1_000_000_000:
        return f"{v / 1_000_000_000:.2f}B"
    if abs(v) >= 1_000_000:
        return f"{v / 1_000_000:.2f}M"
    if abs(v) >= 1_000:
        return f"{v / 1_000:.2f}k"
    return f"{v:.2f}"


def config_path() -> Path:
    return Path.home() / CONFIG_DIR / CONFIG_FILE


def load_config() -> dict:
    p = config_path()
    if not p.exists():
        return {}
    try:
        with open(p, "r", encoding="utf8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(data: dict) -> None:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf8") as f:
        json.dump(data, f, indent=2, sort_keys=True)


# -----------------------------------------------------------------------------
# Seed defaults
# -----------------------------------------------------------------------------


def seed_defaults(registry: Registry) -> None:
    registry.add_chain(Chain("Ethereum", "https://mainnet.example.rpc"))
    registry.add_chain(Chain("Arbitrum", "https://arb.example.rpc", block_time_s=0.25))
    registry.add_chain(Chain("Optimism", "https://op.example.rpc", block_time_s=2.0))
    registry.add_chain(Chain("Polygon", "https://poly.example.rpc", block_time_s=2.0))

    registry.add_protocol(Protocol("AaveV3", "Ethereum", "lending"))
    registry.add_protocol(Protocol("UniswapV3", "Arbitrum", "dex"))
    registry.add_protocol(Protocol("VelodromeV2", "Optimism", "dex"))
    registry.add_protocol(Protocol("CompoundV3", "Ethereum", "lending"))
    registry.add_protocol(Protocol("Curve", "Ethereum", "dex"))

    registry.add_strategy(
        StrategyConfig(
            id="usdc-aave-eth",
            name="USDC Aave V3 Ethereum",
            asset="USDC",
            chain="Ethereum",
            protocol="AaveV3",
            risk_band="CONSERVATIVE",
            base_apr=0.05,
            boost_apr=0.01,
            performance_fee=0.10,
            max_capacity=50_000_000.0,
        )
    )
    registry.add_strategy(
        StrategyConfig(
            id="usdc-uni-arb",
            name="USDC Uniswap V3 Arbitrum",
            asset="USDC",
            chain="Arbitrum",
            protocol="UniswapV3",
            risk_band="BALANCED",
            base_apr=0.12,
            boost_apr=0.03,
            performance_fee=0.15,
            max_capacity=25_000_000.0,
        )
    )
    registry.add_strategy(
        StrategyConfig(
            id="usdc-velo-op",
            name="USDC Velodrome V2 Optimism",
            asset="USDC",
            chain="Optimism",
            protocol="VelodromeV2",
            risk_band="AGGRESSIVE",
            base_apr=0.20,
            boost_apr=0.06,
            performance_fee=0.18,
            max_capacity=15_000_000.0,
        )
    )
    registry.add_strategy(
        StrategyConfig(
            id="usdc-comp-eth",
            name="USDC Compound V3 Ethereum",
            asset="USDC",
            chain="Ethereum",
            protocol="CompoundV3",
            risk_band="CONSERVATIVE",
            base_apr=0.042,
            boost_apr=0.008,
            performance_fee=0.08,
            max_capacity=30_000_000.0,
        )
    )
    registry.add_strategy(
        StrategyConfig(
            id="usdc-curve-eth",
            name="USDC Curve Ethereum",
            asset="USDC",
            chain="Ethereum",
            protocol="Curve",
            risk_band="BALANCED",
            base_apr=0.065,
            boost_apr=0.02,
            performance_fee=0.12,
            max_capacity=25_000_000.0,
        )
    )

    registry.add_vault(
        VaultConfig(
            id="loopa-usdc",
            name="Loopa USDC MetaVault",
            asset="USDC",
            base_chain="Ethereum",
            management_fee=0.02,
            withdrawal_fee=0.001,
            default_risk_band="BALANCED",
            rebalance_interval_s=86400,
            strategies=["usdc-aave-eth", "usdc-uni-arb", "usdc-velo-op", "usdc-comp-eth", "usdc-curve-eth"],
        )
    )


def seed_extended(registry: Registry) -> None:
    """Add more chains, protocols, strategies, and a DAI vault."""
    for name, rpc in [
        ("Base", "https://mainnet.base.org"),
        ("Avalanche", "https://api.avax.network/ext/bc/C/rpc"),
    ]:
        if registry.get_chain(name) is None:
            registry.add_chain(Chain(name, rpc, block_time_s=2.0))

    for chain, proto_name, kind in [
        ("Ethereum", "Morpho", "lending"),
        ("Ethereum", "Yearn", "vault"),
        ("Ethereum", "Convex", "boost"),
        ("Ethereum", "Balancer", "dex"),
        ("Ethereum", "Lido", "staking"),
        ("Polygon", "AaveV3", "lending"),
        ("Base", "Aerodrome", "dex"),
    ]:
        key = f"{chain}:{proto_name}"
        if key not in registry.protocols and registry.get_chain(chain):
            registry.add_protocol(Protocol(proto_name, chain, kind))

    extended_strategies = [
        StrategyConfig(
            id="usdc-morpho-eth",
            name="USDC Morpho Ethereum",
            asset="USDC",
            chain="Ethereum",
            protocol="Morpho",
            risk_band="CONSERVATIVE",
            base_apr=0.055,
            boost_apr=0.012,
            performance_fee=0.10,
            max_capacity=40_000_000.0,
            metadata={"optimizer": "true"},
        ),
        StrategyConfig(
            id="usdc-yearn-eth",
            name="USDC Yearn Ethereum",
            asset="USDC",
            chain="Ethereum",
            protocol="Yearn",
            risk_band="BALANCED",
            base_apr=0.048,
            boost_apr=0.015,
            performance_fee=0.20,
            max_capacity=20_000_000.0,
        ),
        StrategyConfig(
            id="usdc-convex-eth",
            name="USDC Convex Ethereum",
            asset="USDC",
            chain="Ethereum",
            protocol="Convex",
            risk_band="BALANCED",
            base_apr=0.072,
            boost_apr=0.025,
            performance_fee=0.17,
            max_capacity=22_000_000.0,
        ),
        StrategyConfig(
            id="usdc-balancer-eth",
            name="USDC Balancer Ethereum",
            asset="USDC",
            chain="Ethereum",
            protocol="Balancer",
            risk_band="AGGRESSIVE",
            base_apr=0.095,
            boost_apr=0.03,
            performance_fee=0.15,
            max_capacity=18_000_000.0,
        ),
        StrategyConfig(
            id="weth-lido-eth",
            name="WETH Lido Ethereum",
            asset="WETH",
            chain="Ethereum",
            protocol="Lido",
            risk_band="CONSERVATIVE",
            base_apr=0.035,
            boost_apr=0.005,
            performance_fee=0.10,
            max_capacity=100_000.0,
        ),
        StrategyConfig(
            id="usdc-aave-poly",
            name="USDC Aave V3 Polygon",
            asset="USDC",
            chain="Polygon",
            protocol="AaveV3",
            risk_band="CONSERVATIVE",
            base_apr=0.038,
            boost_apr=0.008,
            performance_fee=0.10,
            max_capacity=35_000_000.0,
        ),
        StrategyConfig(
            id="usdc-aero-base",
            name="USDC Aerodrome Base",
            asset="USDC",
            chain="Base",
            protocol="Aerodrome",
            risk_band="AGGRESSIVE",
            base_apr=0.18,
            boost_apr=0.05,
            performance_fee=0.18,
            max_capacity=12_000_000.0,
        ),
        StrategyConfig(
            id="dai-aave-eth",
            name="DAI Aave V3 Ethereum",
            asset="DAI",
            chain="Ethereum",
            protocol="AaveV3",
            risk_band="CONSERVATIVE",
            base_apr=0.045,
            boost_apr=0.01,
            performance_fee=0.10,
            max_capacity=45_000_000.0,
        ),
        StrategyConfig(
            id="dai-comp-eth",
            name="DAI Compound V3 Ethereum",
            asset="DAI",
            chain="Ethereum",
            protocol="CompoundV3",
            risk_band="CONSERVATIVE",
            base_apr=0.04,
            boost_apr=0.008,
            performance_fee=0.08,
            max_capacity=28_000_000.0,
        ),
        StrategyConfig(
            id="dai-curve-eth",
            name="DAI Curve Ethereum",
            asset="DAI",
            chain="Ethereum",
            protocol="Curve",
            risk_band="BALANCED",
            base_apr=0.058,
            boost_apr=0.018,
            performance_fee=0.12,
            max_capacity=20_000_000.0,
        ),
    ]
    for strat in extended_strategies:
        if strat.id not in registry.strategies:
            try:
                registry.add_strategy(strat)
            except ValueError:
                pass

    if "loopa-dai" not in registry.vaults:
        try:
            registry.add_vault(
                VaultConfig(
                    id="loopa-dai",
                    name="Loopa DAI MetaVault",
                    asset="DAI",
                    base_chain="Ethereum",
                    management_fee=0.02,
                    withdrawal_fee=0.001,
                    default_risk_band="CONSERVATIVE",
                    rebalance_interval_s=86400,
                    strategies=["dai-aave-eth", "dai-comp-eth", "dai-curve-eth"],
                )
            )
        except ValueError:
            pass


# -----------------------------------------------------------------------------
# Validator
# -----------------------------------------------------------------------------


class Validator:
    def __init__(self, registry: Registry) -> None:
        self.registry = registry
        self.errors: List[str] = []
        self.warnings: List[str] = []

    def validate_all(self) -> bool:
        self.errors = []
        self.warnings = []
        for chain in self.registry.chains.values():
            self._validate_chain(chain)
        for proto in self.registry.protocols.values():
            self._validate_protocol(proto)
        for strat in self.registry.strategies.values():
            self._validate_strategy(strat)
        for vault in self.registry.vaults.values():
            self._validate_vault(vault)
        return len(self.errors) == 0

    def _validate_chain(self, chain: Chain) -> None:
        if not chain.name or not chain.rpc:
            self.errors.append(f"Chain missing name or rpc: {chain.name}")
        if chain.block_time_s <= 0:
            self.warnings.append(f"Chain {chain.name}: block_time_s should be positive")

    def _validate_protocol(self, proto: Protocol) -> None:
        if proto.chain not in self.registry.chains:
            self.errors.append(f"Protocol {proto.name} references unknown chain {proto.chain}")
        if proto.kind not in ("lending", "dex", "staking", "vault", "boost"):
            self.warnings.append(f"Protocol {proto.name}: unknown kind {proto.kind}")

    def _validate_strategy(self, strat: StrategyConfig) -> None:
        key = f"{strat.chain}:{strat.protocol}"
        if key not in self.registry.protocols:
            self.errors.append(f"Strategy {strat.id} references unknown protocol {key}")
        if strat.base_apr < 0 or strat.boost_apr < 0:
            self.errors.append(f"Strategy {strat.id}: negative APR")
        if strat.performance_fee < 0 or strat.performance_fee > 1:
            self.errors.append(f"Strategy {strat.id}: performance_fee must be in [0,1]")
        if strat.max_capacity <= 0:
            self.warnings.append(f"Strategy {strat.id}: max_capacity should be positive")
        if strat.risk_band not in ("CONSERVATIVE", "BALANCED", "AGGRESSIVE"):
            self.warnings.append(f"Strategy {strat.id}: risk_band {strat.risk_band} non-standard")

    def _validate_vault(self, vault: VaultConfig) -> None:
        if vault.base_chain not in self.registry.chains:
            self.errors.append(f"Vault {vault.id} references unknown chain {vault.base_chain}")
        for sid in vault.strategies:
            if sid not in self.registry.strategies:
                self.errors.append(f"Vault {vault.id} references unknown strategy {sid}")
            else:
                s = self.registry.strategies[sid]
                if s.asset != vault.asset:
                    self.warnings.append(f"Vault {vault.id} asset {vault.asset} vs strategy {sid} asset {s.asset}")
        if vault.management_fee < 0 or vault.management_fee > 1:
            self.errors.append(f"Vault {vault.id}: management_fee must be in [0,1]")
        if vault.rebalance_interval_s <= 0:
            self.warnings.append(f"Vault {vault.id}: rebalance_interval_s should be positive")


# -----------------------------------------------------------------------------
# Report generator
# -----------------------------------------------------------------------------


class ReportGenerator:
    def __init__(self, registry: Registry) -> None:
        self.registry = registry

    def text_report(self, vault_id: Optional[str] = None) -> str:
        lines: List[str] = []
        lines.append(f"# DeployAI Report — {LOOPA_ENGINE} Yield Vaults")
        lines.append(f"Generated at {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}")
        lines.append("")
        lines.append("## Chains")
        for c in self.registry.chains.values():
            lines.append(f"- {c.name}: {c.rpc} (block ~{c.block_time_s}s)")
        lines.append("")
        lines.append("## Strategies (by net APR)")
        sorted_strats = sorted(
            self.registry.strategies.values(),
            key=lambda s: s.net_apr(),
            reverse=True,
        )
        for s in sorted_strats:
            lines.append(f"- **{s.id}**: {s.name} | net APR {fmt_pct(s.net_apr())} | cap {fmt_num(s.max_capacity)} | {s.chain}/{s.protocol}")
        lines.append("")
        lines.append("## Vaults")
        vaults = list(self.registry.vaults.values())
        if vault_id:
            vaults = [v for v in vaults if v.id == vault_id]
        for v in vaults:
            lines.append(f"- **{v.id}**: {v.name} | asset {v.asset} | risk band {v.default_risk_band}")
            lines.append(f"  Strategies: {', '.join(v.strategies)}")
            lines.append(f"  Management fee {fmt_pct(v.management_fee)} | Withdrawal fee {fmt_pct(v.withdrawal_fee)}")
        return "\n".join(lines)

    def apr_comparison_table(self, asset: str = "USDC") -> str:
        lines = [f"## APR comparison ({asset})", ""]
        matching = [s for s in self.registry.strategies.values() if s.asset == asset]
        if not matching:
            return "\n".join(lines) + "No strategies for this asset.\n"
        matching.sort(key=lambda s: s.net_apr(), reverse=True)
        lines.append("| Strategy | Chain | Protocol | Gross APR | Net APR | Cap |")
        lines.append("|---------|-------|----------|-----------|---------|-----|")
        for s in matching:
            lines.append(f"| {s.id} | {s.chain} | {s.protocol} | {fmt_pct(s.gross_apr())} | {fmt_pct(s.net_apr())} | {fmt_num(s.max_capacity)} |")
        return "\n".join(lines)


# -----------------------------------------------------------------------------
# Monte Carlo and stress simulator
# -----------------------------------------------------------------------------


class MonteCarloSimulator:
    def __init__(self, registry: Registry) -> None:
        self.registry = registry

    def run(
        self,
        vault_id: str,
        initial_deposit: float,
        days: int,
        num_paths: int = 100,
        noise_std: float = 0.03,
    ) -> Dict[str, Any]:
        vault = self.registry.get_vault(vault_id)
        if not vault:
            raise ValueError(f"Unknown vault: {vault_id}")
        final_values: List[float] = []
        for _ in range(num_paths):
            sim = Simulator(self.registry)
            res = sim.simulate_vault(vault_id, initial_deposit, days, noise_std=noise_std)
            final_values.append(res.final_value)
        final_values.sort()
        return {
            "vault_id": vault_id,
            "initial_deposit": initial_deposit,
            "days": days,
            "num_paths": num_paths,
            "min_final": min(final_values),
            "max_final": max(final_values),
            "median_final": final_values[num_paths // 2],
            "p5": final_values[max(0, int(num_paths * 0.05))],
            "p95": final_values[min(num_paths - 1, int(num_paths * 0.95))],
            "mean_final": sum(final_values) / len(final_values),
        }

    def stress_test(
        self,
        vault_id: str,
        initial_deposit: float,
        days: int,
        apr_shock: float = -0.50,
    ) -> SimulationResult:
        """Single path with an APR shock (e.g. -50% APR halfway)."""
        vault = self.registry.get_vault(vault_id)
        if not vault:
            raise ValueError(f"Unknown vault: {vault_id}")
        sim = Simulator(self.registry)
        res = sim.simulate_vault(vault_id, initial_deposit, days, noise_std=0.0)
        mid = days // 2
        value_mid = res.initial_deposit + (res.final_value - res.initial_deposit) * (mid / max(days, 1))
        value_end_normal = res.final_value
        shock_mult = 1.0 + apr_shock
        value_end_stress = value_mid + (value_end_normal - value_mid) * shock_mult
        return SimulationResult(
            vault_id=vault_id,
            start_ts=res.start_ts,
            end_ts=res.end_ts,
            initial_deposit=initial_deposit,
            final_value=max(0.0, value_end_stress),
            steps=res.steps,
        )


# -----------------------------------------------------------------------------
# Capacity and health helpers
# -----------------------------------------------------------------------------


def get_total_capacity_by_asset(registry: Registry, asset: str) -> float:
    return sum(s.max_capacity for s in registry.strategies.values() if s.asset == asset)


def get_best_net_apr_strategy(registry: Registry, asset: str, risk_band: Optional[str] = None) -> Optional[StrategyConfig]:
    candidates = [s for s in registry.strategies.values() if s.asset == asset]
    if risk_band:
        candidates = [s for s in candidates if s.risk_band == risk_band]
    if not candidates:
        return None
    return max(candidates, key=lambda s: s.net_apr())


def health_summary(registry: Registry) -> Dict[str, Any]:
    v = Validator(registry)
    v.validate_all()
    return {
        "chains": len(registry.chains),
        "protocols": len(registry.protocols),
        "strategies": len(registry.strategies),
        "vaults": len(registry.vaults),
        "validation_errors": len(v.errors),
        "validation_warnings": len(v.warnings),
        "errors": v.errors,
        "warnings": v.warnings,
    }


def projected_yield_simple(principal: float, apr: float, days: int) -> float:
    """Compound once per day: principal * (1 + apr/365)^days."""
    if days <= 0:
        return principal
    daily = 1.0 + (apr / 365.0)
    return principal * (daily ** days)


def projected_yield_continuous(principal: float, apr: float, days: float) -> float:
    """Continuous compounding: principal * exp(apr * days/365)."""
    if days <= 0:
        return principal
    return principal * math.exp(apr * (days / 365.0))


def strategy_summary_by_chain(registry: Registry) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for s in registry.strategies.values():
        out.setdefault(s.chain, []).append(s.id)
    return out


def strategy_summary_by_protocol(registry: Registry) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for s in registry.strategies.values():
        key = f"{s.chain}:{s.protocol}"
        out.setdefault(key, []).append(s.id)
    return out


def vault_effective_apr_estimate(registry: Registry, vault_id: str) -> Optional[float]:
    """Weighted average net APR of vault's strategies in default risk band."""
    vault = registry.get_vault(vault_id)
    if not vault:
        return None
    total_apr = 0.0
    count = 0
    for sid in vault.strategies:
        s = registry.get_strategy(sid)
        if not s or s.risk_band != vault.default_risk_band:
            continue
        total_apr += s.net_apr()
        count += 1
    if count == 0:
        for sid in vault.strategies:
            s = registry.get_strategy(sid)
            if s:
                total_apr += s.net_apr()
                count += 1
    return total_apr / count if count else None


def list_assets(registry: Registry) -> List[str]:
    assets: set = set()
    for s in registry.strategies.values():
        assets.add(s.asset)
    return sorted(assets)


def list_risk_bands(registry: Registry) -> List[str]:
    bands: set = set()
    for s in registry.strategies.values():
        bands.add(s.risk_band)
    return sorted(bands)


# -----------------------------------------------------------------------------
# CLI: argparse
# -----------------------------------------------------------------------------


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description="DeployAI — Loopa yield vault orchestration and deployment assistant.",
    )
    parser.add_argument("--config", default="", help="Config file path")
    parser.add_argument("command", nargs="?", default="interactive", help="Command to run")
    parser.add_argument("--file", "-f", help="JSON file for load/save")
    parser.add_argument("--vault", default="loopa-usdc", help="Vault ID")
    parser.add_argument("--days", type=int, default=365, help="Simulation days")
    parser.add_argument("--deposit", type=float, default=100_000.0, help="Initial deposit for simulation")
    parser.add_argument("--paths", type=int, default=100, help="Monte Carlo paths")
    parser.add_argument("--asset", default="USDC", help="Asset for APR table / filters")
    parser.add_argument("--version", action="store_true", help="Print version")
    return parser.parse_args(argv)


# -----------------------------------------------------------------------------
# Commands
# -----------------------------------------------------------------------------


def cmd_snapshot(registry: Registry) -> int:
    print(pretty_json(registry.snapshot()))
    return 0


def cmd_strategies(registry: Registry) -> int:
    if not registry.strategies:
        print("No strategies configured.")
        return 0
    for s in registry.strategies.values():
        print(f"  {s.id}: {s.name}  asset={s.asset}  chain={s.chain}  netAPR={fmt_pct(s.net_apr())}  cap={fmt_num(s.max_capacity)}")
    return 0


def cmd_vaults(registry: Registry) -> int:
    if not registry.vaults:
        print("No vaults configured.")
        return 0
    for v in registry.vaults.values():
        print(f"  {v.id}: {v.name}  asset={v.asset}  strategies={', '.join(v.strategies)}")
    return 0


def cmd_chains(registry: Registry) -> int:
    for c in registry.chains.values():
        print(f"  {c.name}: {c.rpc}")
    return 0


def cmd_simulate(registry: Registry, vault_id: str, days: int, deposit: float) -> int:
    sim = Simulator(registry)
    try:
        res = sim.simulate_vault(vault_id, deposit, days)
        print(f"Vault: {res.vault_id}  Initial: {fmt_num(res.initial_deposit)}  Final: {fmt_num(res.final_value)}")
        print(f"Gain: {fmt_pct((res.final_value - res.initial_deposit) / res.initial_deposit)}")
        for step in res.steps[-5:]:
            print(f"  day={step['day']}  value={fmt_num(step['value'])}")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_plan(registry: Registry, vault_id: str) -> int:
    planner = Planner(registry)
    try:
        plan = planner.build_plan(vault_id)
        print(pretty_json(plan.as_dict()))
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_load(registry: Registry, path: str) -> int:
    if not path:
        print("Error: --file required", file=sys.stderr)
        return 1
    if not os.path.exists(path):
        print(f"File not found: {path}", file=sys.stderr)
        return 1
    try:
        registry.load_from_file(path)
        print("Config loaded.")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_save(registry: Registry, path: str) -> int:
    if not path:
        print("Error: --file required", file=sys.stderr)
        return 1
    try:
        with open(path, "w", encoding="utf8") as f:
            json.dump(registry.snapshot(), f, indent=2, sort_keys=True)
        print(f"Saved to {path}")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_demo(registry: Registry) -> int:
    print("DeployAI demo — Loopa yield vault orchestration")
    print("Strategies:", len(registry.strategies))
    print("Vaults:", len(registry.vaults))
    sim = Simulator(registry)
    res = sim.simulate_vault("loopa-usdc", 100_000.0, 90)
    print(f"90-day sim: {fmt_num(res.initial_deposit)} -> {fmt_num(res.final_value)}")
    return 0


def cmd_validate(registry: Registry) -> int:
    v = Validator(registry)
    ok = v.validate_all()
    if v.errors:
        for e in v.errors:
            print(f"Error: {e}", file=sys.stderr)
    if v.warnings:
        for w in v.warnings:
            print(f"Warning: {w}", file=sys.stderr)
    if ok:
        print("Validation passed.")
    else:
        print("Validation failed.", file=sys.stderr)
    return 0 if ok else 1


def cmd_report(registry: Registry, vault_id: Optional[str] = None) -> int:
    gen = ReportGenerator(registry)
    print(gen.text_report(vault_id))
    return 0


def cmd_apr_table(registry: Registry, asset: str) -> int:
    gen = ReportGenerator(registry)
    print(gen.apr_comparison_table(asset))
    return 0


def cmd_monte_carlo(registry: Registry, vault_id: str, days: int, deposit: float, paths: int) -> int:
    mc = MonteCarloSimulator(registry)
    try:
        out = mc.run(vault_id, deposit, days, num_paths=paths)
        print(f"Monte Carlo ({paths} paths) — Vault: {vault_id}  Days: {days}  Deposit: {fmt_num(deposit)}")
        print(f"  Min final: {fmt_num(out['min_final'])}  Max final: {fmt_num(out['max_final'])}")
        print(f"  Median: {fmt_num(out['median_final'])}  Mean: {fmt_num(out['mean_final'])}")
        print(f"  5th pct: {fmt_num(out['p5'])}  95th pct: {fmt_num(out['p95'])}")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_stress(registry: Registry, vault_id: str, days: int, deposit: float, shock: float = -0.50) -> int:
    mc = MonteCarloSimulator(registry)
    try:
        res = mc.stress_test(vault_id, deposit, days, apr_shock=shock)
        print(f"Stress test (APR shock {shock:.0%}) — Vault: {res.vault_id}")
        print(f"  Initial: {fmt_num(res.initial_deposit)}  Final: {fmt_num(res.final_value)}")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_health(registry: Registry) -> int:
    h = health_summary(registry)
    print("Chains:", h["chains"], "Protocols:", h["protocols"], "Strategies:", h["strategies"], "Vaults:", h["vaults"])
    print("Validation errors:", h["validation_errors"], "warnings:", h["validation_warnings"])
    for e in h["errors"]:
        print("  Error:", e)
    for w in h["warnings"]:
        print("  Warning:", w)
    return 0


# -----------------------------------------------------------------------------
# Interactive menu
# -----------------------------------------------------------------------------


def run_interactive(registry: Registry) -> int:
    sim = Simulator(registry)
    planner = Planner(registry)
    while True:
        print()
        print("=== DeployAI :: Loopa Orchestrator ===")
        print("1) Snapshot (JSON)")
        print("2) List strategies")
        print("3) List vaults")
        print("4) List chains")
        print("5) Simulate vault")
        print("6) Build deployment plan")
        print("7) Load config from file")
        print("8) Save snapshot to file")
        print("9) Demo simulation")
        print("10) Validate config")
        print("11) Text report")
        print("12) APR comparison table")
        print("13) Monte Carlo simulation")
        print("14) Stress test")
