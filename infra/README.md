# Aegis Infrastructure — Local Chaos-Testing Environment

This is the **local chaos-testing environment** (ESD §18): a `kind` cluster running Meridian
Commerce's simulated services plus `kube-prometheus-stack`, where failures are deliberately injected
to generate realistic incidents for Aegis to investigate.

## Prerequisites

- Docker Desktop running
- `kind`, `kubectl`, `helm` binaries. They need not be on PATH; pass them via env vars, e.g.:
  ```bash
  export KIND="$HOME/bin/kind.exe"
  export HELM="$HOME/bin/helm.exe"
  export KUBECTL="/c/Program Files/Docker/Docker/resources/bin/kubectl.exe"
  ```

## Bring it up

```bash
bash infra/setup-cluster.sh
```

This creates the `aegis` cluster, builds+loads the Meridian image, installs kube-prometheus-stack,
and deploys the three services with a ServiceMonitor. When it finishes:

- Prometheus → http://localhost:9090
- Alertmanager → http://localhost:9093

Verify Meridian is being scraped:
```bash
curl -s 'http://localhost:9090/api/v1/query?query=up{namespace="meridian"}' | python -m json.tool
```

## Services (Meridian Commerce)

One image (`infra/meridian/`), parameterised by `SERVICE_NAME`, backs three Deployments:
`checkout-service`, `payment-service`, `catalog-service`. Each continuously simulates traffic and
exposes Prometheus metrics (`http_requests_total`, `http_request_duration_seconds`, `app_up`,
`app_injected_error_rate`) plus an admin endpoint to inject faults.

## Inject a failure (generate an incident)

```bash
bash infra/inject-failure.sh error   checkout-service 0.6   # 60% error rate
bash infra/inject-failure.sh latency payment-service 1200   # +1200ms latency
bash infra/inject-failure.sh killpod checkout-service       # delete a pod
bash infra/inject-failure.sh clear   checkout-service       # recover
```

## Security posture (MVP)

The Kubernetes MCP server uses a dedicated ServiceAccount (`aegis-k8s-mcp`) bound to a **read-only**
Role in the `meridian` namespace: `get/list/watch` on pods, pod logs, events, services, deployments,
replicasets — **no write verbs** (ESD §16, PRD NFR-Security). Write verbs are a V1.5 addition.

## Mint the k8s MCP server credential (Milestone 3+)

The backend's k8s MCP server authenticates with the `aegis-k8s-mcp` ServiceAccount token, never
the admin kubeconfig. Mint the git-ignored token + CA files (and get the `K8S_API_URL` value):

```bash
bash infra/gen-mcp-credentials.sh          # writes infra/.k8s-mcp-token + infra/.k8s-mcp-ca.crt
```

Tokens are short-lived (default 24h); re-run when expired. `DURATION=1h` etc. to override.

## Tear down

```bash
"$KIND" delete cluster --name aegis
```
