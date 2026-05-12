# StratEdge — Professional Backtesting Platform

A TradingView-style backtesting web app for NSE/BSE stocks and indices.
Strategy: EMA + RSI crossover with configurable parameters, 1:N risk-reward,
covering 261 stocks across NIFTY50, NIFTY100, sectoral indices, BSE top stocks.

---

## Architecture

```
Browser (TradingView-style UI)
       │
       ▼ HTTP / Ingress
Kubernetes (nginx Ingress)
       │
       ▼
Flask App (Gunicorn, Python 3.11)
       │
       ├── yfinance → Yahoo Finance (10yr daily/weekly/monthly, 60d intraday)
       ├── Disk cache (PVC) — 6hr TTL per symbol+interval
       └── stocks.json — 261 stock universe
```

---

## Project Structure

```
stratedge/
├── app.py                  ← Flask backend (all API endpoints + backtest engine)
├── requirements.txt
├── stocks.json             ← 261 stocks: NSE equities, indices, BSE top
├── templates/
│   └── index.html          ← Full TradingView-style frontend (single file)
├── Dockerfile              ← Multi-stage build (python:3.11-slim)
├── .dockerignore
├── Jenkinsfile             ← CI/CD: build → push → k8s deploy → verify
├── k8s/
│   ├── namespace.yaml      ← stratedge namespace
│   ├── pvc.yaml            ← 5Gi PVC for cache (you create the PV)
│   ├── deployment.yaml     ← 1 replica, resource limits, probes
│   ├── service.yaml        ← ClusterIP :80 → :5000
│   └── ingress.yaml        ← nginx Ingress, IP-based access
└── README.md
```

---

## Local Dev (no Docker/K8s)

### Prerequisites
- Python 3.11+
- Internet access (yfinance fetches from Yahoo Finance)

### Run

```bash
# 1. Clone and enter the project
git clone <your-repo-url> stratedge
cd stratedge

# 2. Create virtual environment
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Run the dev server
python app.py
# or with debug mode:
DEBUG=true python app.py

# 5. Open in browser
http://localhost:5000
```

---

## Docker (local test before K8s)

```bash
# Build
docker build -t stratedge:latest .

# Run
docker run -p 5000:5000 \
  -v $(pwd)/cache:/data/cache \
  stratedge:latest

# Open
http://localhost:5000
```

---

## Kubernetes Deployment (local cluster)

### Step 1 — Install nginx Ingress Controller

```bash
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.10.1/deploy/static/provider/baremetal/deploy.yaml

# Wait for it to be ready
kubectl wait --namespace ingress-nginx \
  --for=condition=ready pod \
  --selector=app.kubernetes.io/component=controller \
  --timeout=120s
```

### Step 2 — Create a local Docker registry (if you don't have one)

```bash
docker run -d -p 5001:5000 --name registry registry:2
# Note: using port 5001 externally to avoid conflict with the app's port 5000
```

If using a registry on port 5001, update `REGISTRY` in Jenkinsfile to `localhost:5001`.

### Step 3 — Create the PersistentVolume

Create the directory on your node first:

```bash
mkdir -p /data/stratedge-cache
```

Then apply this PV (save as `k8s/pv.yaml` — not committed since it's node-specific):

```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: stratedge-cache-pv
spec:
  capacity:
    storage: 5Gi
  accessModes:
    - ReadWriteOnce
  persistentVolumeReclaimPolicy: Retain
  hostPath:
    path: /data/stratedge-cache
  storageClassName: manual
```

```bash
kubectl apply -f k8s/pv.yaml
```

### Step 4 — Apply manifests manually (first time)

```bash
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/pvc.yaml
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/service.yaml
kubectl apply -f k8s/ingress.yaml
```

### Step 5 — Build and push image manually (first time)

```bash
docker build -t localhost:5001/stratedge:1 .
docker push localhost:5001/stratedge:1
kubectl set image deployment/stratedge stratedge=localhost:5001/stratedge:1 -n stratedge
kubectl rollout status deployment/stratedge -n stratedge
```

### Step 6 — Get access URL

```bash
# Get your node IP
kubectl get nodes -o wide

# Get the Ingress NodePort for port 80
kubectl get svc -n ingress-nginx ingress-nginx-controller
# Look for the NodePort under PORT(S), e.g. 80:31234/TCP → port 31234

# Access the app
http://<node-ip>:31234
```

---

## Jenkins CI/CD Setup

### Prerequisites on Jenkins

1. Jenkins installed (with Git plugin)
2. Docker installed on the Jenkins agent, and jenkins user in docker group:
   ```bash
   sudo usermod -aG docker jenkins
   sudo systemctl restart jenkins
   ```
3. kubectl installed on Jenkins agent with access to your cluster:
   ```bash
   # Test from Jenkins agent
   kubectl get nodes
   ```

### Create the Jenkins Pipeline Job

1. Open Jenkins → **New Item** → **Pipeline** → Name it `stratedge`
2. Under **Pipeline** section → **Definition**: `Pipeline script from SCM`
3. **SCM**: Git → enter your repo URL
4. **Branch**: `*/main` (or your branch)
5. **Script Path**: `Jenkinsfile`
6. Save → **Build Now** for first run

### Configure Registry in Jenkinsfile

Edit `Jenkinsfile` line:
```groovy
REGISTRY = 'localhost:5001'   // local Docker registry
// OR for Docker Hub:
REGISTRY = 'docker.io/yourusername'
// OR for a private registry:
REGISTRY = '192.168.1.x:5001'
```

If using Docker Hub, add credentials in Jenkins:
- **Manage Jenkins** → **Credentials** → **Global** → **Add Credentials**
- Kind: Username with password, ID: `dockerhub-creds`
- Then add to Jenkinsfile before push:
  ```groovy
  withCredentials([usernamePassword(credentialsId: 'dockerhub-creds', ...]){
      sh "docker login -u ${DOCKER_USER} -p ${DOCKER_PASS}"
  }
  ```

### How the Pipeline Works

Every push to `main`:
1. **Checkout** — pulls latest code
2. **Lint** — validates `app.py` syntax and `stocks.json`
3. **Build Image** — `docker build` with build number tag + `latest`
4. **Push Image** — pushes both tags to registry
5. **Apply Manifests** — `kubectl apply` all k8s YAMLs (idempotent)
6. **Deploy** — `kubectl set image` → rolls out new image
7. **Verify** — waits for rollout + confirms ready replicas
8. On **failure** — auto rollback to previous deployment

---

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/` | Web UI |
| `GET` | `/health` | Health check + stock count |
| `GET` | `/api/search?q=DABUR&limit=8` | Search stocks by name or symbol |
| `GET` | `/api/data?symbol=DABUR.NS&interval=1d` | Fetch OHLCV + EMA + RSI |
| `POST` | `/api/backtest` | Run backtest for one stock |
| `POST` | `/api/batch` | Batch backtest a group (returns summary table) |
| `GET` | `/api/groups` | List all available groups with counts |
| `POST` | `/api/cache/clear` | Clear all cached data |

### POST /api/backtest

```json
{
  "symbol":        "DABUR.NS",
  "interval":      "1d",
  "ema_period":    5,
  "rsi_period":    14,
  "rsi_threshold": 30,
  "rr_ratio":      3.0,
  "capital":       100000
}
```

### POST /api/batch

```json
{
  "group":         "NIFTY50",
  "interval":      "1d",
  "ema_period":    5,
  "rsi_period":    14,
  "rsi_threshold": 30,
  "rr_ratio":      3.0,
  "capital":       100000
}
```

---

## Strategy Logic

The EMA + RSI Mean-Reversion setup:

| Step | Condition | Action |
|------|-----------|--------|
| Signal day | `High < EMA(5)` AND `RSI(14) < 30` | Mark signal, set pending |
| Next day(s) | Signal carries forward only if `High < EMA` AND `RSI < threshold` still hold | Otherwise, cancel signal |
| Entry | Price breaks above signal day's High (gap up → open price; normal → signal high) | Enter long |
| Exit: SL | Price hits signal day's Low | Close position |
| Exit: Target | Price hits `Entry + RR × Risk` | Close position |

---

## Intervals & Data Coverage

| Interval | Data Range | Notes |
|----------|-----------|-------|
| 1D Daily | Up to 10 years | Full history |
| 1W Weekly | Up to 10 years | Full history |
| 1M Monthly | Up to 10 years | Full history |
| 1H Hourly | Last 60 days | Yahoo Finance limit |
| 15m | Last 60 days | Yahoo Finance limit |

---

## Stock Universe (261 stocks)

| Group | Count |
|-------|-------|
| NIFTY50 | 50 |
| NIFTY Next 50 | 47 |
| NIFTY 100 | 97 |
| NIFTY Bank | 12 |
| NIFTY IT | 10 |
| NIFTY Pharma | 14 |
| NIFTY FMCG | 14 |
| NIFTY Auto | 14 |
| NIFTY Metal | 10 |
| NIFTY Realty | 10 |
| NIFTY Energy | 12 |
| All Indices | 19 |
| BSE Top | 30 |

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `5000` | Server port |
| `CACHE_DIR` | `./cache` | Cache directory path |
| `STOCKS_FILE` | `./stocks.json` | Stock universe file |
| `CACHE_TTL_HOURS` | `6` | Cache TTL in hours |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `WORKERS` | `2` | Gunicorn workers |
| `DEBUG` | `false` | Flask debug mode (dev only) |

---

## Troubleshooting

**yfinance returns no data for a symbol**
- NSE stocks need `.NS` suffix: `DABUR.NS`
- BSE stocks need `.BO` suffix: `DABUR.BO`
- Indices: `^NSEI` (Nifty 50), `^NSEBANK` (Bank Nifty), `^BSESN` (Sensex)
- Try hitting `POST /api/cache/clear` then retry

**Pod in CrashLoopBackOff**
```bash
kubectl logs -l app=stratedge -n stratedge --previous
kubectl describe pod -l app=stratedge -n stratedge
```

**PVC in Pending state**
- Make sure the PV exists and its `storageClassName` matches `manual`
- Check: `kubectl get pv,pvc -n stratedge`

**Ingress not reachable**
```bash
kubectl get svc -n ingress-nginx    # confirm controller is running
kubectl get ingress -n stratedge    # check ADDRESS field
kubectl describe ingress stratedge -n stratedge
```

**Jenkins can't push to local registry**
- Add `localhost:5001` to Docker insecure registries:
  ```json
  // /etc/docker/daemon.json
  { "insecure-registries": ["localhost:5001"] }
  ```
  Then `sudo systemctl restart docker`
