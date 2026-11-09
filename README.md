# Japan Quake Monitor

Real-time seismic dashboard for Japan — a single static HTML file that pulls JMA earthquake reports (relayed by the P2PQuake community network) and the USGS feed directly in the browser, and plots every event on a dark Leaflet map. No build step, no API keys.

The same file runs three ways: hosted on GitHub Pages, as a container image in Kubernetes, or straight off disk in a browser. Optionally, a small **Python backend (FastAPI + SQLite)** sits behind it and keeps a **permanent, searchable earthquake archive** with statistics and CSV export — the dashboard's 📜 History panel talks to it, and it degrades gracefully to standalone mode when no backend is configured.

## Repository layout

```
index.html                  the whole dashboard (HTML/CSS/JS, self-contained)
Dockerfile                  nginx (unprivileged) serving the page on 8080
nginx.conf                  gzip, no-cache for the app shell, /healthz probe endpoint
backend/main.py             FastAPI backend: SQLite archive, collector, history/stats/CSV API
backend/Dockerfile          python:3.12-slim image serving the API + dashboard on 8000
docker-compose.yml          local run: frontend (8080) + backend (8000)
.github/workflows/deploy.yml  CI: builds BOTH images to ghcr.io AND publishes GitHub Pages
k8s/deployment.yaml         frontend Deployment (2 replicas) + Service
k8s/backend.yaml            backend Deployment (1 replica) + PVC (1 Gi) + Service
k8s/ingress.yaml            optional Ingress for external access
```

## 1. GitHub repository

The repository lives at **https://github.com/jpjpjunior/japan-quake-monitor** (public), and GitHub Pages is already enabled with Source: “GitHub Actions” — no manual setup steps remain. If you ever need to push again from a fresh copy of these files:

```bash
cd japan-quake-monitor
git remote add origin https://github.com/jpjpjunior/japan-quake-monitor.git
git push -u origin main
```

After the push, the workflow automatically:

- builds both images and publishes them to `ghcr.io/jpjpjunior/japan-quake-monitor:latest` (frontend) and `ghcr.io/jpjpjunior/japan-quake-monitor-backend:latest` (backend), each plus a `sha-<commit>` tag,
- publishes the dashboard at `https://jpjpjunior.github.io/japan-quake-monitor/`.

If the `pages` job failed because Pages wasn't enabled yet, just re-run it: Actions → the run → “Re-run failed jobs”.

## 2. Local run

Any of these:

```bash
# with the backend — dashboard http://localhost:8080 · API http://localhost:8000/api/healthz
docker compose up --build
# or frontend only
docker build -t japan-quake-monitor . && docker run -p 8080:8080 japan-quake-monitor
# or backend only (archive + stats; serves the dashboard itself on :8000)
DATA_DIR=./data pip install -r backend/requirements.txt && DATA_DIR=./data python backend/main.py
```

## 3. Deploy to Kubernetes

The image is built by Actions, so your cluster just pulls it from GHCR. The manifest already points at your package — just apply:

```bash
kubectl apply -f k8s/deployment.yaml
```

Quick check without an Ingress:

```bash
kubectl get pods -l app=japan-quake-monitor
kubectl port-forward svc/japan-quake-monitor 8080:80
# open http://localhost:8080
```

For external access, edit the host in `k8s/ingress.yaml` and `kubectl apply -f k8s/ingress.yaml` (requires an Ingress controller such as ingress-nginx).

### GHCR package visibility

The first Actions build creates the package **private** by default. Two options:

- **Make it public** (simplest): GitHub → your profile → Packages → `japan-quake-monitor` → Package settings → Change visibility → Public. Nodes then pull it without credentials.
- **Keep it private**: create a pull secret and uncomment the `imagePullSecrets` block in `k8s/deployment.yaml`:

  ```bash
  kubectl create secret docker-registry ghcr-credentials \
    --docker-server=ghcr.io \
    --docker-username=jpjpjunior \
    --docker-password=<a GitHub PAT with read:packages> \
    --docker-email=<your-email>
  ```

### What the Deployment does

- 2 replicas behind a ClusterIP Service, rolling updates with zero downtime.
- `readinessProbe` / `livenessProbe` hit the nginx `/healthz` endpoint, so Kubernetes restarts an unhealthy container automatically.
- Runs as non-root (uid 101), no privilege escalation, all capabilities dropped — appropriate for a static-file server.
- Tiny footprint: requests 32 Mi / 50 m CPU, limits 128 Mi / 200 m CPU.

## 4. Backend service (optional): permanent archive, statistics, CSV

The frontend works standalone, but it can hydrate itself from — and browse — a long-term archive kept by the backend:

- **Collector** (inside the backend): polls the P2PQuake history every 45 s and USGS every 60 s — comfortably inside their documented rate limits — and upserts every earthquake report into SQLite (WAL mode) on a PersistentVolume. Known ids are updated in place, so later JMA announcements refine the magnitude/intensity of the same report. Raw upstream messages are stored too, for future features.
- **API** (CORS open, JSON):
  - `GET /api/healthz` — status, event count, last ingest times
  - `GET /api/events?days=30&limit=500&minmag=4&source=JMA&q=福島` — filtered history, returned in the dashboard's own event format
  - `GET /api/stats?days=30` — per-day counts, intensity histogram, most-affected prefectures, strongest event
  - `GET /api/export.csv?days=365` — spreadsheet export
  - `GET /` — serves the dashboard itself (same-origin mode: zero configuration)

### Deploy

```bash
kubectl apply -f k8s/backend.yaml    # PVC (1 Gi) + Deployment (1 replica) + Service :8000
kubectl get pods -l app=japan-quake-monitor-backend
```

- One replica by design — SQLite is a single-writer database, and the PVC keeps the archive across restarts. If you outgrow it, swap SQLite for Postgres first, then scale.
- **Connecting the dashboard**: served *by the backend* (e.g. an Ingress host pointing at the backend Service), the dashboard connects automatically. From GitHub Pages or the nginx container, open 📜 History once and enter the backend URL (stored in localStorage) — pages served over https need an https backend.
- To try it quickly: `kubectl port-forward svc/japan-quake-monitor-backend 8000:8000` → http://localhost:8000

## Notes

- In the **frontend**, all upstream connections (P2PQuake, USGS, OpenStreetMap/CARTO tiles, Leaflet CDN) are made by the **visitor's browser** — the nginx pods need no outbound internet access. The **backend collector** is the exception: it polls P2PQuake and USGS itself, so the backend pod needs outbound HTTPS.
- The in-page 🔍 Diagnostics panel can always tell you which of those sources your network can reach.
- Data source: JMA earthquake information relayed by P2PQuake, and USGS. The community relay may lag or drop messages — this is not an official warning system; always follow JMA / local authorities.

## Ideas for the next iterations

- **Go port** of the backend — the API contract is frozen (`/api/*`), so `backend/` can be swapped for a Go service as a learning exercise without touching the frontend.
- **Notifications**: Telegram/Discord push on strong quakes, driven by the backend archive (it already stores EEW-flagged raw messages).
- **Replay & heat maps**: animate an event's felt-area reports over time; intensity heat layers from the stored per-prefecture scales.
- **Postgres**: swap storage if you want multiple collector replicas or a multi-region setup.
