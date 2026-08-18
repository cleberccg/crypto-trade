"""Debug registry discovery for ReversaoNextGenV1."""

from strategies.registry import discover_strategies, _REGISTRY, list_registered_strategies

# Discover all strategies
discover_strategies()

print("Total strategies in registry:", len(_REGISTRY))
print("\nAll registry keys:")
for key in sorted(_REGISTRY.keys()):
    spec = _REGISTRY[key]
    print(f"  {key:30} → {spec.name} (v{spec.version})")

print("\nLooking for 'reversao' variations:")
for key in _REGISTRY.keys():
    if "reversao" in key.lower() or "h27" in key:
        spec = _REGISTRY[key]
        print(f"  Found: {key} → {spec.name}")

print("\nLooking for 'reversion' variations:")
for key in _REGISTRY.keys():
    if "reversion" in key.lower():
        spec = _REGISTRY[key]
        print(f"  Found: {key} → {spec.name}")

print("\n\nAll registered strategies:")
for strategy_info in list_registered_strategies():
    print(f"  {strategy_info['name']:30} v{strategy_info['version']:5} family={strategy_info['family']}")
