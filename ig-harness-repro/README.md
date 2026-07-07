# Harness `${...}` JEXL expression collision — reproduction

This repo reproduces the issue where a ForgeRock IG route condition

```json
"condition": "${not empty request.headers['client_id'][0]}"
```

is silently rewritten to

```json
"condition": "false"
```

**by Harness** (not by Helm) when deployed with a **Kubernetes** deployment type.

## Root cause (short version)

Harness renders manifests by passing the text inside `${...}` to its **JEXL** engine
(`${...}` is Harness's legacy FirstGen expression delimiter and collides with IG's
`${...}`). JEXL's `empty` operator is **null-safe**, so with `request` undefined:

```
not empty request.headers['client_id'][0]
  -> empty(null) = true
  -> not true    = false
```

The expression fully resolves, so Harness replaces it with `false`.

Every other condition survives because JEXL either **throws** (comparison/property
access on an unknown variable, e.g. `request.method == 'POST'`) or hits an **unknown
function** (`matches(...)`) — and Harness leaves unresolved `${...}` strings intact.

## What's in here

```
helm/
  Chart.yaml
  values.yaml                     # 1 route, 4 conditions (1 broken, 2 controls, 1 fixed)
  templates/
    _helpers.tpl                  # stub helm.prefix / helm.labels (swap for your real ones)
    configmap_routes_all.yaml     # your ConfigMap-per-partition template
harness/
  service.yaml                    # Kubernetes deploy type, Helm chart from Git
  environment.yaml                # PreProduction env
  infrastructure.yaml             # KubernetesDirect infra
  pipeline.yaml                   # Deployment stage with K8sDryRun (+ optional rollout)
```

## Step 1 — Prove Helm is innocent (local)

```bash
helm template repro helm | grep '"condition"'
```

All 4 conditions are preserved verbatim (works on Helm v3 and v4).

## Step 2 — Reproduce in Harness

1. Push this repo to Git.
2. Create/confirm a **Git connector** (to this repo) and a **Kubernetes cluster connector**.
3. Import the YAML (or recreate via UI):
   - `harness/service.yaml`
   - `harness/environment.yaml`
   - `harness/infrastructure.yaml`
   - `harness/pipeline.yaml`
   Replace every `<+input>` (org/project/connector refs) with your values.
4. Run the pipeline. The **K8s Dry Run** step output shows the rendered ConfigMap.

### Expected Harness output (the bug)

```json
"condition": "false",                                                   // 10-repro-broken: not empty ...  -> REWRITTEN
"condition": "${request.method == 'POST'}",                             // preserved
"condition": "${matches(request.headers['Authorization'][0], '^Bearer (.*)')}",  // preserved
"condition": "${request.headers['client_id'][0] != null && request.headers['client_id'][0] != ''}"  // preserved (the fix)
```

Only the `not empty` line flips to `false`.

## Step 3 — The fix

Replace the `not empty` form with a null/empty comparison that IG understands but
JEXL cannot resolve (so Harness leaves it alone):

```json
// before (breaks under Harness)
"condition": "${not empty request.headers['client_id'][0]}"

// after (survives Harness, valid ForgeRock IG EL)
"condition": "${request.headers['client_id'][0] != null && request.headers['client_id'][0] != ''}"
```

The `values.yaml` already includes this fixed form as the 4th filter so you can see
it pass through untouched in the same run.

## Notes

- Use **Kubernetes** deployment type to reproduce. Harness runs `helm template` then
  applies its own JEXL rendering on the result — that second step is the culprit.
- The `<+input>` placeholders are runtime inputs; set them at run time or hardcode them.
- If your real chart splits routes into multiple partitions, remember the
  `kubectl apply` (client-side) `last-applied-configuration` annotation limit is
  **256 KB** — keep each ConfigMap under it or use server-side apply.
