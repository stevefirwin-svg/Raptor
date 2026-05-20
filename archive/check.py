from config import CONFIG
r = CONFIG.risk
# Print hold monitor layer weights
import json
h = json.load(open('hold_health.json'))
intc = h.get('INTC', {})
print('=== LAYER WEIGHTS ===')
for layer, data in intc.get('layers', {}).items():
    print(layer.ljust(20) + ' weight=' + str(data['weight']) + '  score=' + str(round(data['score'],3)))
