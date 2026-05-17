# StratEdge Risk Calculator — Microservice

Advanced Trading Risk Calculator — independent microservice running
alongside the existing StratEdge backtester on Kubernetes.

## Architecture

```
Browser
  │
  ▼
Ingress (nginx)
  │
  ├── /               → stratedge (existing backtest app)
  └── /risk-calculator → risk-calculator (this service)
```

## Features

| Calculator | Endpoint |
|---|---|
| Position Size | POST /api/risk/position-size |
| Risk/Reward | POST /api/risk/rr |
| Stop Loss | POST /api/risk/stop-loss |
| Leverage | POST /api/risk/leverage |
| Futures Liquidation | POST /api/risk/liquidation |
| Portfolio Risk | POST /api/risk/portfolio |
| Drawdown | POST /api/risk/drawdown |
| Trade Journal | GET/POST /api/risk/journal |
| CSV Export | GET /api/risk/export/csv |
| JSON Export | GET /api/risk/export/json |

## Quick Deploy

```bash
# 1. Build image
cd risk-calculator/
docker build -t risk-calculator:v1 .

# 2. Verify image
docker run --rm risk-calculator:v1 python -c "
import urllib.request
print(urllib.request.urlopen('http://localhost:6000/health').read())
"

# 3. Apply all K8s manifests
kubectl apply -f k8s/deployment.yaml -n stratedge
kubectl apply -f k8s/manifests.yaml  -n stratedge

# 4. Verify
kubectl get pods -n stratedge -l app=risk-calculator
kubectl logs -f deploy/risk-calculator -n stratedge

# 5. Open in browser
# https://stratedge.ocpv.citiuscloud.local/risk-calculator
```

## Helm Deploy

```bash
helm install risk-calculator ./helm \
  --namespace stratedge \
  --set secrets.JWT_SECRET="your-secret" \
  --set secrets.SECRET_KEY="your-flask-key"

# Upgrade
helm upgrade risk-calculator ./helm --namespace stratedge
```

## Local Development

```bash
# Docker Compose (fastest)
docker-compose up --build

# Direct Python
pip install -r requirements.txt
cd app && python main.py
# Opens on http://localhost:6000/risk-calculator
```

## Run Tests

```bash
pip install -r requirements.txt pytest
pytest tests/ -v
```

## API Examples

### Position Size
```bash
curl -X POST http://localhost:6000/api/risk/position-size \
  -H "Content-Type: application/json" \
  -d '{"account_size":100000,"risk_pct":1,"entry":500,"stop_loss":490}'
```

### Risk/Reward
```bash
curl -X POST http://localhost:6000/api/risk/rr \
  -H "Content-Type: application/json" \
  -d '{"entry":500,"stop_loss":485,"target":545,"shares":100}'
```

### Portfolio Risk
```bash
curl -X POST http://localhost:6000/api/risk/portfolio \
  -H "Content-Type: application/json" \
  -d '{
    "account_size": 500000,
    "positions": [
      {"symbol":"INFY","entry":1500,"current":1560,"shares":100,"stop_loss":1450},
      {"symbol":"TCS","entry":3500,"current":3420,"shares":50,"stop_loss":3400}
    ]
  }'
```

### Add Journal Trade
```bash
curl -X POST http://localhost:6000/api/risk/journal \
  -H "Content-Type: application/json" \
  -d '{"symbol":"RELIANCE","direction":"BUY","entry":2500,"exit":2600,"shares":10,"date":"2026-05-17","strategy":"EMA+RSI"}'
```

## Integrate into Existing StratEdge Navbar

Add this link to your existing `index.html` topbar:

```html
<a href="/risk-calculator" class="tb-btn" target="_blank">
  📐 Risk Calc
</a>
```

Or embed as iframe:

```html
<iframe
  src="/risk-calculator"
  style="width:100%;height:100vh;border:none"
  title="Risk Calculator">
</iframe>
```

## Monitoring

- Health:   GET /health
- Metrics:  GET /metrics  (Prometheus format)

Prometheus scrape config:
```yaml
- job_name: risk-calculator
  static_configs:
    - targets: ['risk-calculator-svc.stratedge.svc.cluster.local:6000']
```

## Production Security Checklist

```bash
# Change secrets from defaults
kubectl create secret generic risk-calculator-secret \
  --from-literal=JWT_SECRET="$(openssl rand -hex 32)" \
  --from-literal=SECRET_KEY="$(openssl rand -hex 32)" \
  -n stratedge --dry-run=client -o yaml | kubectl apply -f -
```
