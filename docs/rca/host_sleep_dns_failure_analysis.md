# 💤 Host Sleep / DNS Resolution Failure Analysis

This document provides a comprehensive post-mortem of the recurring issue where the `sre-controller` (and other cluster components) fail to communicate with the Kubernetes API after the host machine goes to sleep or is suspended.

---

## 1. The Symptoms

When returning to the project after the host OS (Linux/Windows) has been asleep, you will typically observe the following cascading failures:

1.  **Broken Port-Forwards**: Any active `kubectl port-forward` commands (like Grafana or Prometheus) will abruptly drop or hang.
2.  **Controller API Errors**: The `sre-controller` logs will flood with `ClientConnectorError` messages:
    ```
    Request attempt #1/9 failed; will retry: GET https://kubernetes.default.svc/api -> 
    ClientConnectorError(..., gaierror(-3, 'Temporary failure in name resolution'))
    ```
3.  **Grafana "No Data"**: Grafana dashboards will stop updating, or display "No data" because the underlying pods have restarted and the Prometheus data source connections have severed.

---

## 2. The Root Cause

The root cause lies in how **Docker** and **Kind (Kubernetes-in-Docker)** handle host machine sleep states.

When a computer goes to sleep, its physical network interfaces (Wi-Fi, Ethernet) are powered down. When the machine wakes up:
*   The host OS dynamically requests a new IP or refreshes its network state.
*   However, the Docker daemon's internal virtual networks (`docker0` bridge) often **do not sync** this state change correctly.
*   Because `kind` runs Kubernetes nodes as Docker containers, the `CoreDNS` pods inside the cluster end up with a stale DNS cache or a broken routing table. They literally lose the ability to resolve internal cluster DNS names like `kubernetes.default.svc`.
*   Consequently, the `sre-controller` cannot talk to the API Server, causing `kopf` to continuously throw connection errors.

---

## 3. How We Overcame It (The Remediation Playbook)

To fix this issue without completely destroying and rebuilding the `kind` cluster, we executed a 4-step remediation process to flush the stale network states from the bottom up.

### Step 1: Force a Docker Network Refresh on the Nodes
The fastest way to force Docker to rebuild the virtual network interfaces for the `kind` cluster is to restart the worker node containers directly from the host.
```bash
docker restart sre-agent-cluster-worker sre-agent-cluster-worker2
```
*(Note: This temporarily disrupts the cluster as the nodes reboot).*

### Step 2: Flush the Kubernetes DNS Cache
Once the nodes are back online, we force Kubernetes to deploy fresh `CoreDNS` pods, which guarantees they boot with the newly refreshed Docker networking state.
```bash
kubectl delete pods -n kube-system -l k8s-app=kube-dns
```

### Step 3: Bounce the Controller
Because the `sre-controller` was stuck in a retry-loop trying to hit a dead DNS endpoint, we delete its pod to force a clean startup sequence.
```bash
kubectl delete pod -n monitoring -l app=sre-controller
```
When the new controller pod boots up, it will query the fresh CoreDNS pods, successfully resolve `kubernetes.default.svc`, and authenticate with the API server.

### Step 4: Re-establish Tunnels and Trigger Metrics
Finally, because the node restart killed all active connections, we must restart our local tunnels:
```bash
kubectl port-forward -n observability svc/grafana 3000:3000 > /dev/null 2>&1 &
kubectl port-forward -n observability svc/prometheus 9090:9090 > /dev/null 2>&1 &
```
*Because the pods restarted, their in-memory metrics reset. To repopulate Grafana, we simply triggered a new crash (`kubectl delete pod -n production -l app=crash-demo`) to force a fresh LLM diagnosis and emit new metrics.*

---

## 4. Long-Term Prevention

If this becomes too disruptive during development:
1.  **Prevent Sleep**: The easiest solution is to prevent the host machine (especially if it's a Linux server or VM) from sleeping while the `kind` cluster is active.
2.  **Automated Script**: Create a bash script (`wake-cluster.sh`) containing the four remediation commands above, so you can instantly restore the cluster state in 10 seconds whenever you wake your computer.
