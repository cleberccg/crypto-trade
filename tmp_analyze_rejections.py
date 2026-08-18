import json

with open('optimization/results/campanha_limpa_mysql_20260701_200645.json', 'r') as f:
    data = json.load(f)

# Procura rejection_reasons em strategies
print('=== REJECTION PATTERNS ===\n')
rejection_counts = {}
rejections_list = []

for strat in data.get('strategies', []):
    if 'rejection_reason' in strat:
        reason = strat['rejection_reason']
        rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
        rejections_list.append({
            'name': strat['name'],
            'reason': reason,
            'score': strat.get('score_fila', 0)
        })

# Sort by rejection reason and print
for entry in sorted(rejections_list, key=lambda x: x['reason']):
    print(f"{entry['name']:40} | score={entry['score']:6.2f} | {entry['reason']}")

print('\n=== SUMMARY ===')
for reason, count in sorted(rejection_counts.items(), key=lambda x: -x[1]):
    print(f'{count:2d}x {reason}')
