#!/usr/bin/env python3
import json

manifest = json.load(open('data/aggtrades/manifest.json'))

# Remove BTCUSDT|2026-02 devido a um problema persistente de qualidade dos dados
if 'BTCUSDT|2026-02' in manifest['partitions']:
    del manifest['partitions']['BTCUSDT|2026-02']
    
    with open('data/aggtrades/manifest.json', 'w') as f:
        json.dump(manifest, f, indent=2, default=str)
    
    print('✓ Deleted BTCUSDT|2026-02 from manifest')
    print(f'Total valid partitions now: {len(manifest["partitions"])}')
    
    # Conta por símbolo
    btc = sum(1 for k in manifest['partitions'] if 'BTCUSDT' in k)
    eth = sum(1 for k in manifest['partitions'] if 'ETHUSDT' in k)
    print(f'BTCUSDT: {btc}, ETHUSDT: {eth}')
else:
    print('BTCUSDT|2026-02 not found (may have been previously deleted)')
