# Japan Quake Monitor

Real-time seismic dashboard for Japan — a single static HTML file that pulls JMA earthquake reports (relayed by the P2PQuake community network) and the USGS feed directly in the browser, and plots every event on a dark Leaflet map. No backend, no database, no API keys.

The same file runs three ways: hosted on GitHub Pages, as a container image in Kubernetes, or straight off disk in a browser.

## Repository layout

```
index.html                  the whole dashboard (HTML/CSS/JS, self-contained)
Dockerfile                  nginx (unprivileged) serving the page on 8080
nginx.conf                  gzip, no-cache for the app shell, /healthz probe endpoint
docker-compose.yml          local container run
.github/workflows/deploy.yml  CI: builds the image to ghcr.io AND publishes GitHub Pages
k8s/deployment.yaml         Kubernetes Deployment (2 replicas) + Service
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

- builds the container image and publishes it to `ghcr.io/jpjpjunior/japan-quake-monitor:latest` (plus a `sha-<commit>` tag),
- publishes the dashboard at `https://jpjpjunior.github.io/japan-quake-monitor/`.

If the `pages` job failed because Pages wasn't enabled yet, just re-run it: Actions → the run → “Re-run failed jobs”.

## 2. Local run

Any of these:

```bash
# plain static server, no container
python3 -m http.server 8080

# container
docker compose up --build          # → http://localhost:8080
# or
docker build -t japan-quake-monitor . && docker run -p 8080:8080 japan-quake-monitor
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

## Notes

- All upstream connections (P2PQuake relay for JMA data, USGS, OpenStreetMap/CARTO tiles, Leaflet CDN) are made by the **visitor's browser**, not by the container or the cluster — the pods never need outbound internet access.
- The in-page 🔍 Diagnostics panel can always tell you which of those sources your network can reach.
- Data source: JMA earthquake information relayed by P2PQuake, and USGS. The community relay may lag or drop messages — this is not an official warning system; always follow JMA / local authorities.
