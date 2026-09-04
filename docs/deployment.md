# Deployment

Running the server for real, on each platform. Architecture is in
[architecture.md](architecture.md).

## Ubuntu

Ubuntu is the primary target. 22.04 ships Python 3.10 and 24.04 ships 3.12 — both fine.
On 20.04 the system Python is 3.8, which is below the floor; add the deadsnakes PPA
(`sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt install python3.11 python3.11-venv`)
and run `make install PY=python3.11`.

```bash
sudo apt update && sudo apt install -y python3-venv python3-pip make
git clone <your-remote> llmserve && cd llmserve      # or copy the folder across
make install
make test
make run                                             # mock backend on :8000
```

`make help` lists every target. In a second shell:

```bash
make smoke                                           # one streamed completion
make load-sweep                                      # closed loop
make load-open                                       # open loop -- shows shedding
```

### Running it as a service

For anything long-lived, run it under systemd rather than a terminal — you get restarts,
journald logs, and a stop path that respects the graceful drain.

```bash
sudo useradd --system --home /opt/llmserve --shell /usr/sbin/nologin llmserve
sudo mkdir -p /opt/llmserve /etc/llmserve
sudo cp -r . /opt/llmserve && sudo chown -R llmserve:llmserve /opt/llmserve
sudo -u llmserve make -C /opt/llmserve install

sudo cp deploy/llmserve.env.example /etc/llmserve/llmserve.env
sudo chmod 640 /etc/llmserve/llmserve.env             # it holds API keys and HF tokens
sudo nano /etc/llmserve/llmserve.env                  # set backend, model, limits

sudo cp deploy/llmserve.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now llmserve

systemctl status llmserve
journalctl -u llmserve -f                             # structured JSON, one line per request
```

The unit is written around this server's shutdown semantics: `KillSignal=SIGTERM` triggers
the drain, and `TimeoutStopSec=180` gives in-flight generations room to finish streaming —
keep it above `LLMSERVE_DRAIN_TIMEOUT_S`, which in turn must exceed your longest
generation. `TimeoutStartSec=900` exists because loading a large model takes minutes and
systemd would otherwise kill it mid-load.

`systemctl reload-or-restart` is a hard cut. For a zero-drop restart, drain first:

```bash
curl -XPOST localhost:8000/admin/drain    # /readyz flips to 503, in-flight work continues
sleep 30                                  # let your load balancer notice
sudo systemctl restart llmserve
```

Put nginx or Caddy in front for TLS. One thing that will bite you: **turn off proxy
buffering**, or SSE arrives in one lump at the end.

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_buffering off;          # required for streaming
    proxy_read_timeout 600s;      # longer than your longest generation
    proxy_set_header X-Request-Id $request_id;
}
```

### Docker

```bash
docker compose up --build server              # CPU / mock
docker compose --profile gpu up --build server-gpu
```

The GPU profile needs the NVIDIA Container Toolkit:

```bash
sudo apt install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

### GPU prerequisites

```bash
ubuntu-drivers devices            # see what your card wants
sudo ubuntu-drivers install       # or: sudo apt install nvidia-driver-<version>
sudo reboot
nvidia-smi                        # must list the GPU before going further

pip install -r requirements-gpu.txt   # vLLM brings its own pinned CUDA torch build
```

You do **not** need a separate CUDA toolkit install — the vLLM wheels bundle what they
need; the driver is the only system-level requirement.

## Running against a real model

```bash
pip install -r requirements-gpu.txt        # on a CUDA host
LLMSERVE_BACKEND=vllm \
LLMSERVE_MODEL=meta-llama/Llama-3.1-8B-Instruct \
LLMSERVE_MAX_CONCURRENT_REQUESTS=32 \
LLMSERVE_MAX_QUEUE_SIZE=128 \
python -m llmserve
```

or `make docker-gpu` (`Dockerfile.gpu`, weights on a mounted `/models` volume).

vLLM does its own continuous batching; this server's job is to bound *how many* requests
reach it (`max_num_seqs` is wired to `max_concurrent_requests`) and to make everything
past that bound explicit rather than emergent.

---

## Graceful shutdown

On SIGTERM the lifespan stops admitting, rejects anything queued with 503, waits up to
`drain_timeout_s` for in-flight generations to finish streaming, then tears the engine
down. Verified end to end: a request in flight when SIGTERM lands still returns 200 with
all of its tokens.

Kubernetes removes a pod from the Service and sends SIGTERM at roughly the same moment,
which races. Use `/admin/drain` as a `preStop` hook so readiness flips first:

```yaml
lifecycle:
  preStop:
    exec:
      command: ["curl", "-fsS", "-XPOST", "http://127.0.0.1:8000/admin/drain"]
readinessProbe:
  httpGet: { path: /readyz, port: 8000 }
livenessProbe:
  httpGet: { path: /healthz, port: 8000 }
terminationGracePeriodSeconds: 120   # ≥ drain_timeout_s + longest generation
```

---

## WSL

WSL is the better place to run this from Windows: `make` works, and it is the only way
to run the real vLLM backend on this machine.

**One trap first.** The Windows and Linux virtualenvs cannot share the `.venv` directory
(`Scripts\python.exe` vs `bin/python`). And working directly in `/mnt/d/...` is slow —
WSL reaches NTFS through a translation layer, and creating a venv there takes minutes
instead of seconds. So copy the project onto the Linux filesystem:

```bash
sudo apt update && sudo apt install -y python3-venv python3-pip make
cp -r "/mnt/d/Self-Hosted LLM Serving Platform" ~/llmserve
cd ~/llmserve
make install
make test
make run
```

If you would rather work in place on the D: drive, give the Linux venv its own name so it
does not collide with the Windows one — the Makefile takes `VENV` as a variable:

```bash
cd "/mnt/d/Self-Hosted LLM Serving Platform"
make install VENV=.venv-linux
make test VENV=.venv-linux
make run VENV=.venv-linux
```

The server binds `0.0.0.0`, and WSL2 forwards localhost, so `http://127.0.0.1:8000` works
from a Windows browser or PowerShell while the server runs in WSL. Load-test from either
side:

```bash
make load-sweep            # closed loop
make load-open             # open loop -- shows admission control shedding
```

### Real vLLM on the GPU

WSL2 is the supported path for CUDA on Windows. Two rules:

* the NVIDIA driver goes on **Windows**, not inside WSL — installing a Linux driver in the
  distro breaks the passthrough;
* check it landed before installing anything: `nvidia-smi` inside WSL should list your GPU.

Then:

```bash
pip install -r requirements-gpu.txt          # several GB of torch/vLLM wheels

LLMSERVE_BACKEND=vllm \
LLMSERVE_MODEL=Qwen/Qwen2.5-0.5B-Instruct \
LLMSERVE_VLLM_GPU_MEMORY_UTILIZATION=0.80 \
LLMSERVE_MAX_CONCURRENT_REQUESTS=16 \
make run
```

Start with a small model to prove the path end to end, then scale up. Two WSL-specific
knobs matter:

* **`gpu_memory_utilization`** should be lower than on a headless Linux box — Windows'
  display driver reserves VRAM, so vLLM's view of "free" is optimistic. 0.80 is a safe
  first try; raise it until vLLM stops complaining about KV-cache space.
* **WSL gets ~50% of your RAM by default.** Loading weights is CPU-memory hungry; if the
  distro dies during load, raise the limit in `C:\Users\<you>\.wslconfig`:

  ```ini
  [wsl2]
  memory=24GB
  swap=8GB
  ```

  then `wsl --shutdown` from PowerShell and reopen the distro.

Once it is up, everything else is identical — same endpoints, same `/metrics`, same load
harness. Re-run `make load-sweep` against the real engine and compare the knee to the
mock numbers above; that comparison is the whole point of having both backends.

## Windows / PowerShell

`make` is not available on Windows, so use the task runner instead — it calls
`.venv\Scripts\python.exe` directly, so you never need to activate the virtualenv:

```powershell
cd "D:\Self-Hosted LLM Serving Platform"
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass   # this session only
.\scripts\tasks.ps1 install
.\scripts\tasks.ps1 test
.\scripts\tasks.ps1 run                                     # Ctrl+C to stop
```

Then in a **second** PowerShell window:

```powershell
cd "D:\Self-Hosted LLM Serving Platform"
.\scripts\tasks.ps1 smoke                                   # one streamed completion
.\scripts\tasks.ps1 sweep                                   # closed-loop concurrency sweep
.\scripts\tasks.ps1 open                                    # open-loop, shows shedding
```

`.\scripts\tasks.ps1 demo` does the whole thing in one window: boots the server, streams a
completion, overloads it, prints the admission counters, then shuts it down.
`.\scripts\tasks.ps1` on its own lists every task.

Prefer raw commands? These are the equivalents:

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest -q
$env:LLMSERVE_BACKEND = "mock"; $env:LLMSERVE_PORT = "8000"
.venv\Scripts\python.exe -m llmserve
.venv\Scripts\python.exe -m loadtest --rps 5,10,20,40 --duration 15
```

Two Windows gotchas worth knowing:

* **`curl` in PowerShell is an alias for `Invoke-WebRequest`**, which buffers the whole
  response — so SSE looks like it arrives all at once. Use `curl.exe` (note the `.exe`)
  for real streaming, or `scripts\stream_client.py`, which streams properly on any
  platform and prints TTFT and mean inter-token latency.
* **vLLM does not run natively on Windows.** The `mock` backend does everything above on
  Windows; for the real engine use WSL2, a Linux host, or `Dockerfile.gpu`.
