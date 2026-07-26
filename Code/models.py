"""Model registry: what is staged on disk, and which one the pipeline uses.

WHY A REGISTRY RATHER THAN JUST AN ENV VAR
  D4 makes the pipeline model-agnostic via LLM_MODEL, and the demo's strongest
  beat is swapping models live to show that a bigger model hallucinates too.
  That beat needs the swap to be visible and one click away, not an SSH session
  and a restart.

HONESTY (doc 1.6a) -- read before touching this file
  Selecting a model here changes which model the NEXT extraction calls. It does
  NOT retroactively change contracts already extracted: every contract row
  stores the model that actually produced it, and the UI shows that per row.
  A model listed as `staged` is present on disk; `serving` means the endpoint
  currently answers as that model. We never claim a model ran if it did not.

The selection is stored on disk (not in memory) so it survives a restart, and
every change is written to the audit chain.
"""
import json
import os
import re

MODELS_DIR = os.environ.get(
    "LEDGER_MODELS_DIR",
    "/home/dell/Desktop/Dell-Hackathon-Obligation-Ledger-AI-07-26-26/Models")
STATE_PATH = os.environ.get("LEDGER_MODEL_STATE",
                            "/srv/ledger/data/model_state.json")
ENDPOINT = os.environ.get("LLM_URL",
                          "http://inference.local/v1/chat/completions")


def served():
    """Ask the endpoint what it is actually serving.

    vLLM's --served-model-name is set at container launch and frequently does
    NOT match the HuggingFace repo id (ours serves 'qwen3-35b' from the
    Qwen3.6-35B-A3B-FP8 weights). Sending the repo id would 404, so the served
    id is discovered rather than assumed. Never raises; [] means unreachable.
    """
    import json as _json
    import urllib.error
    import urllib.request
    probe = ENDPOINT.replace("/chat/completions", "/models")
    try:
        with urllib.request.urlopen(probe, timeout=2) as r:
            payload = _json.loads(r.read())
    except (urllib.error.URLError, OSError, ValueError):
        return []
    out = []
    for m in payload.get("data", []):
        if m.get("id"):
            out.append({"id": m["id"], "root": m.get("root"),
                        "max_model_len": m.get("max_model_len")})
    return out

# Facts sourced from MASTER doc 3.2 and Appendix B.2, which cite published
# benchmarks. Every number here has a source in the master document; do not
# add a figure without one.
CATALOG = {
    "Qwen/Qwen3.6-35B-A3B-FP8": {
        "dir": "Qwen3.6-35B-A3B-FP8",
        "label": "Qwen3.6 35B-A3B",
        "quant": "FP8",
        "role": "primary",
        "params": "35B total / 3B active",
        "experts": "256 experts, 8 routed + 1 shared",
        "throughput": "~28-30 tok/s single-user",
        "note": "Doc's primary. Conservative, verified vLLM recipe.",
    },
    "nvidia/Qwen3.6-35B-A3B-NVFP4": {
        "dir": "Qwen3.6-35B-A3B-NVFP4",
        "label": "Qwen3.6 35B-A3B",
        "quant": "NVFP4",
        "role": "fastest",
        "params": "35B total / 3B active",
        "experts": "256 experts, 8 routed + 1 shared",
        "throughput": "~97 tok/s single-stream",
        "note": "Blackwell-native. Needs vLLM >= 0.24.0.",
    },
    "openai/gpt-oss-120b": {
        "dir": "gpt-oss-120b",
        "label": "gpt-oss 120B",
        "quant": "MXFP4",
        "role": "largest",
        "params": "120B total / ~5B active",
        "experts": "128 experts, 4 per token",
        "throughput": "fits GB10 unified memory",
        "note": "The live-swap demo model. Bigger, still hallucinates.",
    },
    "openai/gpt-oss-20b": {
        "dir": "gpt-oss-20b",
        "label": "gpt-oss 20B",
        "quant": "MXFP4",
        "role": "fallback",
        "params": "20B total / ~3.6B active",
        "experts": "32 experts, 4 per token",
        "throughput": "~70 tok/s",
        "note": "Fallback if the primary misbehaves.",
    },
}


def _disk_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _mounted_dirs():
    """Which weights directory each running vLLM container has mounted.

    vLLM reports `root` as the in-container path (typically a generic
    '/model'), which cannot identify the weights. The container's bind mount
    can: it names the exact directory on disk. This is the authoritative link
    between "what is being served" and "which of our staged models it is".

    Returns {served_id_hint: dirname}. Never raises; {} if docker is
    unavailable, in which case we fall back to the name heuristic.
    """
    import subprocess
    try:
        out = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=5, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    found = {}
    for cname in [c for c in out.split() if c]:
        try:
            info = subprocess.run(
                ["docker", "inspect", cname, "--format",
                 "{{range .Mounts}}{{.Source}}\n{{end}}{{join .Args \" \"}}"],
                capture_output=True, text=True, timeout=5, check=True).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        lines = [l for l in info.splitlines() if l.strip()]
        if not lines:
            continue
        args = lines[-1]
        m = re.search(r"--served-model-name[= ]([^\s]+)", args)
        if not m:
            continue
        for src in lines[:-1]:
            found[m.group(1)] = os.path.basename(src.rstrip("/"))
    return found


def _match_served(name, meta, live):
    """Map a catalog entry to the id the endpoint will accept, if any."""
    ids = {s["id"] for s in live}
    # 1. authoritative: the container mounted exactly these weights
    for served_id, dirname in _mounted_dirs().items():
        if served_id in ids and dirname == meta["dir"]:
            return served_id
    # 2. vLLM reported a real weights path as root
    for s in live:
        root = (s.get("root") or "").rstrip("/")
        if root and os.path.basename(root) == meta["dir"]:
            return s["id"]
    return None


def staged(live=None):
    """Catalog models present on disk, with real sizes and the served id.

    `served_as` is the string to actually send to the endpoint; None means the
    endpoint is not serving these weights right now, so selecting it would 404.
    """
    if live is None:
        live = served()
    out = []
    for name, meta in CATALOG.items():
        path = os.path.join(MODELS_DIR, meta["dir"])
        present = os.path.isdir(path)
        entry = dict(meta, name=name, present=present, path=path)
        if present:
            entry["bytes"] = _disk_size(path)
            entry["gb"] = round(entry["bytes"] / 1e9, 1)
            entry["shards"] = sum(1 for f in os.listdir(path)
                                  if f.endswith(".safetensors"))
        entry["served_as"] = _match_served(name, meta, live)
        entry["live"] = entry["served_as"] is not None
        out.append(entry)
    order = {"primary": 0, "fastest": 1, "largest": 2, "fallback": 3}
    # live first, then staged-on-disk, then role
    out.sort(key=lambda e: (not e["live"], not e["present"],
                            order.get(e["role"], 9)))
    return out


def wire_name(name=None):
    """The exact string to send as `model` in a completions request.

    Falls back to the catalog name when nothing is served, so replay/record
    still record something meaningful.
    """
    name = name or selected()
    for e in staged():
        if e["name"] == name:
            return e["served_as"] or name
    return name


DEFAULT_MODEL = "Qwen/Qwen3.6-35B-A3B-FP8"


def selected():
    """The catalog name of the model the next extraction will request.

    Always a CATALOG key. $LLM_MODEL is honoured only if it names one: a wire
    id like 'qwen3-35b' leaking into this variable used to be returned verbatim,
    which dropped it out of the catalog and made the UI render the model's
    quantisation as "undefined". A value we cannot describe is not a selection.
    """
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as fh:
                name = json.load(fh).get("model")
            if name in CATALOG:
                return name
        except (OSError, ValueError):
            pass
    env = os.environ.get("LLM_MODEL")
    if env in CATALOG:
        return env
    # $LLM_MODEL may be a served id; map it back to the catalog entry it came
    # from rather than returning something the UI cannot label.
    if env:
        for entry in staged():
            if entry.get("served_as") == env:
                return entry["name"]
    return DEFAULT_MODEL


def select(name):
    """Set the model for subsequent extractions. Refuses unknown names."""
    if name not in CATALOG:
        raise ValueError(f"unknown model: {name}")
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"model": name}, fh)
    os.replace(tmp, STATE_PATH)          # atomic; a crash cannot half-write it
    return name


def describe(name):
    meta = CATALOG.get(name)
    if not meta:
        return {"name": name, "label": name, "known": False}
    return dict(meta, name=name, known=True)


if __name__ == "__main__":
    cur = selected()
    print(f"selected: {cur}\n")
    for m in staged():
        mark = "*" if m["name"] == cur else " "
        if m["present"]:
            print(f" {mark} {m['label']:<20} {m['quant']:<7} {m['gb']:>5} GB  "
                  f"{m['shards']:>3} shards  {m['role']}")
        else:
            print(f" {mark} {m['label']:<20} {m['quant']:<7}   NOT STAGED")
