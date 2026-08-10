# Root Cause Analysis: sre-controller DNS Resolution Failure

## 1. Incident Summary
Following a restart of the physical host server (which consequently restarts the local Kubernetes `kind` cluster), the `sre-controller` pod repeatedly failed to connect to the Kubernetes API server. 

The controller logs were flooded with the following error:
`ClientConnectorError(ConnectionKey(host='kubernetes.default.svc', port=443, ...), gaierror(-3, 'Temporary failure in name resolution'))`

This prevented the `sre-controller` from watching pod statuses and triggering the Multi-Agent Remediation Pipeline for any failures in the cluster.

## 2. Impact
- **Severity**: High
- **Impact**: Total failure of the auto-remediation pipeline. The agentic infrastructure was blind to any new or existing pod failures in the cluster until manually intervened.

## 3. Root Cause Investigation

The investigation uncovered a combination of three factors that created a perfect storm for this persistent failure:

### A. The Startup Race Condition
When the physical server restarts, Docker attempts to bring up all `kind` cluster containers simultaneously. Because the `sre-controller` is a lightweight Python container, it starts up incredibly fast—often faster than the cluster's internal `CoreDNS` pods. As a result, the controller's initial attempt to resolve the cluster-internal domain `kubernetes.default.svc` fails.

### B. Negative DNS Caching (`aiohttp`)
The controller relies on the `kopf` framework, which uses `aiohttp` for asynchronous HTTP requests. When `aiohttp` encounters a DNS lookup failure (the `gaierror -3`), it relies on the underlying system resolver. Due to Alpine/Slim Linux libc constraints and specific implementations of `getaddrinfo`, this negative DNS result was effectively cached or trapped in a retry-loop that never re-queried a fully initialized `CoreDNS`.

### C. The Hardcoded Kopf Fallback (The True Root Cause)
The question remained: *Why was the controller trying to resolve `kubernetes.default.svc` via DNS in the first place?*

Kubernetes natively injects the exact IP address of the API server into every pod using the environment variables `KUBERNETES_SERVICE_HOST` (e.g., `10.96.0.1`) and `KUBERNETES_SERVICE_PORT`. Official Kubernetes client libraries use these variables automatically, avoiding DNS entirely.

However, `sre-controller` only had the asynchronous client (`kubernetes_asyncio`) installed. The `kopf` operator framework attempts to load the official synchronous library (`kubernetes`) to handle authentication. When it fails to find it (resulting in an `ImportError`), `kopf` silently falls back to a minimalistic internal login handler called `login_with_service_account`.

Upon inspecting the `kopf` source code, we found that this fallback handler **hardcodes** the API address:
```python
# From kopf/engines/posting.py
return credentials.ConnectionInfo(
    server='https://kubernetes.default.svc', # <--- Hardcoded string
    ...
)
```
Because of this hardcoded string, `kopf` completely ignored the robust Kubelet-provided environment variables and forced a DNS lookup, which crashed head-first into the race condition.

## 4. Resolution

### Temporary Mitigation (Discarded)
Initially, a workaround was attempted by adding a `hostAliases` block to the `sre-controller` deployment manifest to force the container's `/etc/hosts` file to map `kubernetes.default.svc` to `10.96.0.1`. While this worked, it was a brittle hack that did not address the underlying framework behavior.

### Permanent Solution
The permanent fix required intercepting `kopf`'s authentication mechanism to force it to use the Kubelet environment variables.

A custom `@kopf.on.login()` override was implemented directly in `controller/main.py`:

```python
@kopf.on.login()
async def login_fn(**kwargs):
    # Retrieve the service account token...
    
    # Use the bulletproof environment variables provided by Kubelet
    host = os.environ.get("KUBERNETES_SERVICE_HOST")
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    
    # Fallback to default only if env vars are mysteriously missing
    server = f"https://{host}:{port}" if host else 'https://kubernetes.default.svc'

    return kopf.ConnectionInfo(
        server=server,
        token=token,
        # ...
    )
```

By explicitly supplying the IP-based server URL (e.g., `https://10.96.0.1:443`), we completely bypassed the need for DNS resolution during the critical authentication phase. 

## 5. Lessons Learned
1. **Beware Framework Defaults**: Frameworks often have internal fallbacks (like `kopf`'s minimalistic login) that make assumptions (like hardcoded DNS names) which may not hold true in all deployment scenarios (like rapid local cluster restarts).
2. **Prioritize Injected Infrastructure Variables**: For critical control-plane connectivity (like talking to the K8s API), always prefer deterministic, injected configuration (like `KUBERNETES_SERVICE_HOST`) over dynamically resolved configuration (DNS).
