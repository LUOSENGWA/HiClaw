# External S3-Compatible Storage Backend (`storage.provider=s3`)

AgentTeams can use an external S3-compatible object store as its storage
backend instead of the embedded MinIO. The chart ships MinIO as the
default; this document covers the optional `s3` provider, verified today
against **RustFS 1.0.3** (see [compatibility matrix](#verified-compatibility)).

## What `provider=s3` means

| Aspect | `minio` (default) | `oss` (external cloud) | `s3` (external self-hosted) |
|---|---|---|---|
| Engine lifecycle | StatefulSet in this chart | external, cloud-managed | external, you operate it |
| Credentials | static root (chart secret) | STS via credential-provider sidecar | fixed root (chart secret or existingSecret) |
| Bucket creation | controller (EnsureBucket) | external | **controller (EnsureBucket)** |
| Worker users/policies | controller via `mc admin` | external | **controller via `mc admin`** |
| Credential sidecar | no | yes | no |

The backend must speak **S3** and the **MinIO-compatible admin API**
(`mc admin user/policy`): the controller manages per-worker credentials
with the `mc` binary bundled in the controller image. RustFS implements
both (verified; note RustFS duplicate operations are idempotent no-ops
rather than errors, which the controller's idempotent admin client
handles in both styles).

## Verified compatibility

Claims in this document are scoped to the versions and deployment modes
actually exercised:

| Component | Version | Notes |
|---|---|---|
| AgentTeams | `main` @ `6ede5977` (v1.2.5) + this PR | K8s chart install |
| `mc` (controller-embedded & test) | `RELEASE.2025-08-13T08-35-41Z` (commit `7394ce0d`) | same binary the controller image ships |
| RustFS | `1.0.3` (`rustfs/rustfs:1.0.3`, amd64/arm64) | single-node, local disk; single-node on NFS loopback |
| MinIO (migration source) | `RELEASE.2025-09-07` | single-node, migration source only |
| Test env | Raspberry Pi 5 (arm64), Docker | full migrate→verify→cutover E2E, 200 MiB / 100 objects / 3 users / 7 policies |

Not yet verified: multi-node RustFS, RustFS on NFS in production mode
(see RustFS docs; treat as risk), other S3 engines (S3-compatible
engines must pass the [reproducible test suite](#tests) before use).

## Enabling it

```yaml
# values.yaml
storage:
  provider: s3
  mode: existing
  bucket: agentteams-storage
  s3:
    endpoint: "http://rustfs:9000"      # reachable from the controller pod
    # Preferred: reference an existing Secret
    existingSecret: my-rustfs-creds
    accessKeySecretKey: accesskey
    secretKeySecretKey: secretkey
    # Alternative (dev only): inline credentials, chart renders the secret
    # accessKey: "rustfsadmin"
    # secretKey: "rustfsadmin"
```

`helm install` validates the combination at render time
(`provider=s3` requires `mode=existing`, an endpoint, and credentials).
The MinIO StatefulSet is not rendered and no credential-provider sidecar
is deployed.

## Tests

Two reproducible layers:

1. **Controller admin-lifecycle unit tests** (run in CI, no live engine):
   `agentteams-controller/internal/oss/minio_admin_backend_test.go`
   drives the real `MinIOAdminClient` against a fake `mc` in both
   observed engine behaviours (MinIO error-style idempotency and RustFS
   silent idempotency), asserting the exact `mc admin` call sequence,
   alias reuse, policy document shape, and teardown order.
   `cd agentteams-controller && go test ./internal/oss/`
2. **Helm render tests** (run in CI): `tests/check-helm-agentteams.sh`
   covers the s3 provider render (endpoint/secret/env wiring, no MinIO
   StatefulSet, no sidecar, `existingSecret` + custom key names) and the
   failure modes (missing endpoint / missing credentials /
   `mode=managed`).
3. **End-to-end against real engines**: `test/e2e/external-s3-backend.sh`
   brings up MinIO (source) and RustFS (target) containers and exercises
   the full S3 operation matrix plus the `mc admin` user/policy
   lifecycle on both engines, printing the tested version matrix.
   Requires Docker; see the script header for usage.

## Migrating an embedded MinIO deployment to an external backend

> This section is the operational guide for moving an existing
> AgentTeams installation from embedded MinIO to an external S3 backend
> (e.g. RustFS), including **final data reconciliation** and
> **rollback after writes have begun on the target**.

### Phase overview

```
0. freeze window        stop worker writes (scale down workers / maintenance mode)
1. bulk copy            copy all objects source → target (mc mirror)
2. IAM replay           recreate users/policies on target (mc admin)
3. shadow               re-enable workers pointed at target; source frozen read-only
4. final reconciliation  compare source vs target (count/size/checksum)
5. cutover              flip AgentTeams to provider=s3 (endpoint switch)
6. (if broken)          rollback — see below
```

### 1. Bulk copy

```sh
mc mirror --overwrite --remove "src/agents"  "dst/agents"
mc mirror --overwrite --remove "src/shared"  "dst/shared"
# ... one per bucket (agents / shared / teams / agentteams-config, or the
# bucket list of your deployment)
```

### 2. IAM replay

The AgentTeams controller creates one user per Worker plus a scoped
policy (`worker-<name>`, team- and role-scoped). Replay on the target
with the same semantics the controller uses:

```sh
mc admin user add dst <worker> <password>
mc admin policy create dst worker-<worker> policy.json
mc admin policy attach dst worker-<worker> --user <worker>
```

If you migrate **with** the new chart version, you can instead let the
controller do it: after the endpoint switch, `EnsureUser`/`EnsurePolicy`
recreate everything on first reconcile (idempotent on both engine
behaviours). Pre-replaying gives you a verifiable target before cutover.

### 3–4. Shadow + final data reconciliation

Run workers against the target while the source stays **read-only**.
Reconcile with an independent comparison (not the copy tool):

```sh
# object counts and sizes per bucket
mc ls --recursive --json src/agents | jq -r '[.size] | @csv' | sort > /tmp/src.csv
mc ls --recursive --json dst/agents | jq -r '[.size] | @csv' | sort > /tmp/dst.csv
diff /tmp/src.csv /tmp/dst.csv

# checksum sampling (deterministic object set)
mc ls --recursive src/agents | shuf -n 20 | awk '{print $NF}' | while read -r k; do
  a=$(mc cat "src/agents/$k" | sha256sum | cut -d' ' -f1)
  b=$(mc cat "dst/agents/$k" | sha256sum | cut -d' ' -f1)
  [ "$a" = "$b" ] || echo "MISMATCH $k"
done
```

Cutover gate: zero count/size diffs and zero checksum mismatches.

### 5. Cutover

1. Freeze writes again (short window).
2. Delta sync: `mc mirror --overwrite src/<bucket> dst/<bucket>` (no `--remove`).
3. Reconcile (phase 4 commands) — must be clean.
4. Update the deployment: `storage.provider=s3`, `storage.s3.endpoint=...`,
   credentials secret. `helm upgrade`.
5. Verify: controller log shows `storage provider: s3 (external)`; one
   worker round-trips a file through MinIO.

### Rollback after writes have begun on the target

Risk: once workers write to the target, the target holds objects the
source does not have. A plain "flip the endpoint back" would lose them.

Procedure (RPO = 0 when performed before the next source-side backup
expires; RTO ≈ delta-sync time + cutover):

1. **Stop writes on the target** (freeze workers; scale to zero).
2. **Reverse delta sync** — copy everything the target has back to the
   source. Since the source was frozen read-only during shadow, the
   reverse set is exactly the objects created/modified on the target
   after cutover:

   ```sh
   mc mirror --overwrite dst/<bucket> src/<bucket>   # per bucket
   ```

   (One-way import features such as RustFS on-demand-migration do not
   apply in reverse; `mc mirror` is the supported path here.)
3. **Reconcile source** with the pre-cutover baseline (phase 4 commands,
   source as reference) — confirm the reverse sync restored parity.
4. **Flip back**: `storage.provider=minio`, `helm upgrade`, verify one
   worker round-trip.
5. Keep the target frozen (read-only) for the investigation window;
   wipe or re-sync it when the retry starts.

Rollback before any target write is simpler: flip the endpoint back and
discard the target state — the source is untouched.

### Retention after cutover

Keep the old MinIO (stopped, data retained, image locally saved) for at
least one full backup cycle plus two release windows; take a final
reconciliation snapshot and an archive of the data directory before
decommissioning.
