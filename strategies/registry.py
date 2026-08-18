from __future__ import annotations

from dataclasses import dataclass, field
from importlib import import_module
from inspect import signature, Parameter
from pkgutil import iter_modules
from typing import Any, Callable


@dataclass
class StrategyRegistration:
    name: str
    strategy_cls: type
    version: str = "v1"
    family: str = "generic"
    description: str = ""
    parameters: list[str] = field(default_factory=list)
    indicators: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    compatibility: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    parameter_aliases: dict[str, str] = field(default_factory=dict)


_REGISTRY: dict[str, StrategyRegistration] = {}
_DISCOVERED = False


def _canonical(value: str) -> str:
    return value.strip().lower().replace("_", "")


def register_strategy(
    *,
    name: str,
    version: str,
    family: str,
    description: str,
    parameters: list[str],
    indicators: list[str],
    categories: list[str],
    compatibility: list[str],
    aliases: list[str] | None = None,
    parameter_aliases: dict[str, str] | None = None,
) -> Callable[[type], type]:
    aliases = aliases or []
    parameter_aliases = parameter_aliases or {}

    def _decorator(strategy_cls: type) -> type:
        spec = StrategyRegistration(
            name=name,
            strategy_cls=strategy_cls,
            version=version,
            family=family,
            description=description,
            parameters=parameters,
            indicators=indicators,
            categories=categories,
            compatibility=compatibility,
            aliases=aliases,
            parameter_aliases=parameter_aliases,
        )
        keys = {_canonical(name), *(_canonical(alias) for alias in aliases)}
        for key in keys:
            _REGISTRY[key] = spec
        return strategy_cls

    return _decorator


def discover_strategies() -> None:
    global _DISCOVERED
    if _DISCOVERED:
        return

    package_name = "strategies"
    package = import_module(package_name)
    ignored = {"__init__", "base_strategy", "factory", "registry", "families"}

    for module in iter_modules(package.__path__):
        if module.name in ignored:
            continue
        import_module(f"{package_name}.{module.name}")

    _DISCOVERED = True


def normalize_parameters(spec: StrategyRegistration, parameters: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    alias_map = {str(key): str(value) for key, value in spec.parameter_aliases.items()}

    for key, value in parameters.items():
        if value is None:
            continue
        target = alias_map.get(str(key), str(key))
        normalized[target] = value

    return normalized


def filter_supported_kwargs(strategy_cls: type, parameters: dict[str, Any]) -> dict[str, Any]:
    sig = signature(strategy_cls.__init__)
    accepts_var_kw = any(param.kind == Parameter.VAR_KEYWORD for param in sig.parameters.values())
    if accepts_var_kw:
        return parameters

    allowed = {name for name in sig.parameters.keys() if name != "self"}
    return {k: v for k, v in parameters.items() if k in allowed}


def get_registration(strategy_name: str) -> StrategyRegistration:
    discover_strategies()
    key = _canonical(strategy_name)
    if key not in _REGISTRY:
        raise ValueError(f"Unknown strategy '{strategy_name}'.")
    return _REGISTRY[key]


def list_registered_strategies() -> list[dict[str, Any]]:
    discover_strategies()

    dedup: dict[str, StrategyRegistration] = {}
    for spec in _REGISTRY.values():
        dedup[spec.name] = spec

    payload: list[dict[str, Any]] = []
    for name in sorted(dedup.keys()):
        spec = dedup[name]
        payload.append(
            {
                "name": spec.name,
                "version": spec.version,
                "family": spec.family,
                "description": spec.description,
                "parameters": spec.parameters,
                "indicators": spec.indicators,
                "categories": spec.categories,
                "compatibility": spec.compatibility,
                "aliases": spec.aliases,
            }
        )
    return payload


def list_strategy_families() -> list[dict[str, Any]]:
    discover_strategies()
    family_map: dict[str, list[str]] = {}

    for spec in _REGISTRY.values():
        family_map.setdefault(spec.family, [])
        if spec.name not in family_map[spec.family]:
            family_map[spec.family].append(spec.name)

    return [
        {
            "family": family,
            "strategies": sorted(names),
            "count": len(names),
        }
        for family, names in sorted(family_map.items())
    ]


def family_comparison_snapshot() -> list[dict[str, Any]]:
    discover_strategies()
    by_family: dict[str, dict[str, Any]] = {}

    for spec in _REGISTRY.values():
        node = by_family.setdefault(
            spec.family,
            {
                "family": spec.family,
                "strategies": set(),
                "categories": set(),
                "indicators": set(),
                "compatibility": set(),
            },
        )
        node["strategies"].add(spec.name)
        node["categories"].update(spec.categories)
        node["indicators"].update(spec.indicators)
        node["compatibility"].update(spec.compatibility)

    snapshot: list[dict[str, Any]] = []
    for family, node in sorted(by_family.items()):
        snapshot.append(
            {
                "family": family,
                "strategy_count": len(node["strategies"]),
                "strategies": sorted(node["strategies"]),
                "categories": sorted(node["categories"]),
                "indicators": sorted(node["indicators"]),
                "compatibility": sorted(node["compatibility"]),
            }
        )
    return snapshot
