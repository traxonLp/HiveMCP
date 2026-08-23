# HiveMCP on Kubernetes

```
kubectl apply -f deploy/k8s/
```

The numeric prefixes are load-bearing: `kubectl apply -f <dir>` works through files in
lexical order, and the Deployment refers to the ConfigMap, the Secret and both PVCs.

## Before the first apply

Four things in these files are placeholders and will not work as shipped.

**1. The signing key.** `01-config.yaml` carries `CHANGE-ME-generate-a-real-key`. It
signs download links and iframe tokens, and every replica must agree on it — a link
signed by one pod fails on every other, which presents as downloads that work only
sometimes. Create it out of band so a real key never reaches git:

```
kubectl delete secret hivemcp-secrets --ignore-not-found
kubectl create secret generic hivemcp-secrets \
  --from-literal=HIVE_SIGNING_KEY="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
```

Then delete the `Secret` block from `01-config.yaml` so a later `apply` cannot overwrite
it with the placeholder.

**2. The hostnames.** `HIVE_PUBLIC_URL` in `01-config.yaml` and the `host` in
`03-service.yaml` are two halves of one setting and must match. Downloads are built from
the first and arrive through the second; a mismatch produces links that resolve nowhere,
and it fails in the user's browser rather than in any log here.

**3. `HIVE_OWUI_URL`.** The in-cluster address of OpenWebUI. This is the reverse
direction and is correctly a Service name. HiveMCP validates every session token against
it, so nothing works until it is right.

**4. The namespace selectors** in `05-networkpolicy.yaml`. They fail closed: wrong
labels mean HiveMCP cannot reach OpenWebUI and every call fails authentication. The
policy only does anything at all if your CNI enforces NetworkPolicy — Calico, Cilium and
Antrea do, plain flannel does not.

## Storage

Two volumes with opposite lifecycles. `hivemcp-templates` is small, curated by
administrators and read on nearly every request; `hivemcp-data` churns constantly and is
swept on a TTL. Keeping them apart means the template pool survives clearing the artifact
volume, and a runaway render cannot fill the disk holding the corporate designs.

Both are `ReadWriteMany` so several replicas can share them. **If your storage class only
offers ReadWriteOnce**, set `replicas: 1` in `02-deployment.yaml` and delete the
HorizontalPodAutoscaler from `04-scaling.yaml`.

`fsGroup: 10001` matches the uid baked into the image and is what makes both volumes
writable without root. Changing the uid in the Dockerfile means changing it here too.

## Connecting OpenWebUI

Admin Settings → Integrations → add a connection:

- **MCP (Streamable HTTP)** → `https://<your-host>/mcp`
- or **OpenAPI** → `https://<your-host>/openapi.json`

Set that connection's authentication to **Session**. This is the whole security model:
HiveMCP holds no service credentials, and acts on the Files API with the caller's own
token so generated files belong to the user who asked for them. Without it every call
fails with a message saying so.

The OpenAPI connection is the one that can render the configuration GUI and the download
card as inline cards; the MCP surface cannot, because OpenWebUI's event system is not
available there. Both expose the same tools otherwise.

## Verifying a deployment

```
kubectl get pods -l app.kubernetes.io/name=hivemcp
kubectl exec deploy/hivemcp -- python -c \
  "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8080/readyz').read())"
```

`/readyz` writes a probe file to both volumes and fails if either is not writable, so a
broken mount takes the pod out of the Service instead of failing every render sent to it.

The startup log line names what is configured, including which skills loaded. `NONE
FOUND` there means the Markdown did not make it into the image, and the only other
symptom would be a model that never learned how to call the tools.

## What is deliberately not here

- **No CPU limit.** Rendering is bursty and CPU-bound, and a limit means CFS throttling
  mid-render — a slow document rather than a rejected one, which is harder to diagnose.
  The memory limit stays, so a runaway render is killed rather than taking the node.
- **No `X-Frame-Options`.** The configuration card exists to be embedded in OpenWebUI's
  iframe. Denying framing would switch the feature off rather than protect anything.
- **No ServiceMonitor.** There is no metrics endpoint yet.
