# Vertex AI Integration Troubleshooting & Post-Mortem

During the transition from the local Ollama backend to the **GCP Vertex AI (Gemini)** backend, we encountered a series of cascading infrastructure and dependency issues. This document serves as a post-mortem to explain what went wrong and exactly how we fixed it.

## 1. Dependency Resolution Conflict (`pydantic` & `httpx`)

> [!WARNING]
> **Symptom:** The Docker image build (`docker build`) began failing with `ResolutionImpossible` and `exit code 1` while running `pip install -r requirements.txt`.

**The Root Cause:**
The user introduced the new `google-genai` SDK to interact with Vertex AI. This SDK relies on several foundational Python libraries with strict version constraints:
* It requires newer features of `pydantic` (Specifically `<3.0.0 and >=2.12.5` or `>=2.9.0` depending on the version).
* It requires a newer version of the HTTP client `httpx` (`>=0.28.1`).

Our project's `requirements.txt` had rigidly pinned `pydantic==2.7.4` and older versions of `httpx`. When `pip` attempted to build the environment, it couldn't satisfy both the old pinned version and the new SDK requirements, resulting in a fatal dependency conflict.

**The Fix:**
We manually relaxed and updated the pins in `requirements.txt`:
```diff
- pydantic==2.7.4
+ pydantic>=2.9.0
- httpx (if pinned)
+ httpx>=0.28.1
```
This allowed `pip` to install the modern versions needed by the `google-genai` SDK without breaking the rest of our stack.

---

## 2. Silent Hangs in the K8s Async Client (`aiohttp` Regression)

> [!CAUTION]
> **Symptom:** After successfully building the Docker image, the `sre-controller` pod would completely hang on startup. The logs would stop right after `Listing pods in namespace production...` and then the pod would eventually crash with a `SIGTERM` (liveness probe failure).

**The Root Cause:**
This was an insidious issue caused by a transitive dependency upgrade. 
When we upgraded the dependencies for Vertex AI, `pip` pulled in the latest version of `aiohttp` (v3.14.3) because we had not explicitly pinned it.

The newer versions of `aiohttp` introduced a feature called `aiohappyeyeballs` to manage dual-stack (IPv4/IPv6) connections. In minimal Kubernetes pods (like our Debian-slim image) running inside local clusters, `aiohappyeyeballs` can sometimes get stuck in a silent deadlock when resolving `.svc.cluster.local` DNS names, causing the `kubernetes_asyncio` library to freeze indefinitely when trying to communicate with the K8s API server.

**The Fix:**
We pinned `aiohttp` to a known stable version that predates the `aiohappyeyeballs` regression. We added this to the very top of `requirements.txt`:
```text
aiohttp==3.9.5
```

---

## 3. Kind Cluster Networking Failure (Host Reboot)

> [!IMPORTANT]
> **Symptom:** After fixing the silent hang, the controller logs revealed loud DNS errors: `gaierror(-3, 'Temporary failure in name resolution')` when trying to resolve `kubernetes.default.svc`.

**The Root Cause:**
While we were debugging, the underlying host machine (the server running our IDE/Docker) was rebooted. 
`Kind` (Kubernetes IN Docker) creates a virtual network bridge and configures intricate `iptables` rules on the host to route traffic between the nodes (containers) and the internal CoreDNS pods. 

When a host reboots, the Docker daemon restarts, but the custom `iptables` rules required for Kind's internal routing often get wiped or corrupted. As a result, the controller pod was completely disconnected from the Kubernetes API because the internal CoreDNS service was unreachable.

**The Fix:**
We performed a "hard reset" of the underlying Kind infrastructure without destroying the cluster:
1. **Restarted the Docker Nodes:** We manually restarted the underlying docker containers representing the nodes to force Docker to recreate the bridge networking rules.
   ```bash
   docker restart sre-agent-cluster-control-plane sre-agent-cluster-worker sre-agent-cluster-worker2
   ```
2. **Bounced CoreDNS:** We forced Kubernetes to recreate the DNS pods to ensure they picked up the healthy networking state.
   ```bash
   kubectl rollout restart deployment coredns -n kube-system
   ```

---

## 4. Google Auth Metadata Server Timeouts

> [!NOTE]
> **Symptom:** The `kubernetes_asyncio` client was attempting to load Google Application Default Credentials (ADC) on startup because of the `google.auth` library installed by `google-genai`.

**The Root Cause:**
The `google.auth` library aggressively attempts to fetch default credentials. If it doesn't find them explicitly configured, it makes a blocking network call to the GCP Metadata Server (`169.254.169.254`). Because we are running inside a local Kind cluster (not a real GKE cluster), this network call is blackholed. It doesn't fail immediately; it hangs until a 2-minute TCP timeout is reached, which blocked our asynchronous event loop.

**The Fix (Controller YAML & LLM Client Changes):**
To resolve this, we had to carefully structure how credentials were passed in the `controller-deployment.yaml` and loaded in the code:

1. **Mounting the Secret**: We mounted the `gcp-credentials` Kubernetes secret as a volume in the pod at `/etc/gcp/credentials.json`.
2. **Removing the Global Env Var**: We deliberately **removed** the `GOOGLE_APPLICATION_CREDENTIALS` environment variable from the deployment manifest.
   * *Why?* If we set it globally, the `kubernetes_asyncio` client's built-in Google Auth plugin would detect it on startup and attempt to authenticate using those credentials (which triggers the blocking network call). By omitting it globally, `kubernetes_asyncio` safely defaults to using the standard in-cluster service account token instead.
3. **Lazy Loading in Code**: We updated `llm_client.py` to manually check `/etc/gcp/credentials.json` and inject the environment variable *only* when the Vertex AI client is specifically requested:
   ```python
   adc_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "/etc/gcp/credentials.json")
   if os.path.exists(adc_path) and "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ:
       os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = adc_path
   ```

---

## Summary
By carefully unwinding the dependency tree (`pydantic` / `aiohttp`), adjusting our K8s manifests for authentication, and restoring the underlying Docker network, we successfully stabilized the agent to leverage Google Vertex AI!
