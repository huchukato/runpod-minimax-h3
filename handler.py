"""RunPod serverless handler for the MiniMax-H3 ComfyUI stack.

Boots ComfyUI once per worker, then each job picks a workflow template by name,
patches its widgets (prompt / camera_tag / duration / unet config), uploads any
input media, queues the prompt, and returns the produced media as base64.

Job input:
  {
    "workflow": "MiniMaxH3-Turbo-R2VA-GGUF.json",   # file in /opt/workflows
    "prompt":   "...",                             # enhancer/directive text
    "config":   "prism_r2va_turbo|10eros_fl2va_turbo|hybrid_fl2va_r2va_turbo",
    "camera_tag": "[ORBIT]",                       # optional
    "seconds":  6,                                 # optional duration override
    "images":   ["https://... or base64"],         # optional, fills LoadImage nodes in id order
    "video":    "https://... or base64"            # optional, fills VHS_LoadVideo
  }
"""

import asyncio
import base64
import glob
import json
import os
import random
import re
import shutil
import subprocess
import threading
import time
import urllib.request
import urllib.parse
import uuid
from pathlib import Path

import runpod

COMFY_DIR = os.environ.get("COMFYUI_DIR", "/workspace/runpod-slim/ComfyUI")
COMFY_URL = "http://127.0.0.1:8188"
WORKFLOWS_DIR = Path(os.environ.get("WORKFLOWS_DIR", "/opt/workflows"))
JOB_TIMEOUT_S = int(os.environ.get("JOB_TIMEOUT_S", "1800"))

# Workflows boot-synced from the ForgeHub repo override the baked copies, so a
# workflow edit lands at the next cold boot without rebuilding the image.
FH_REPO = os.environ.get("FORGEHUB_REPO", "huchukato/ForgeHub")
FH_BRANCH = os.environ.get("FORGEHUB_BRANCH", "main")
FH_ENDPOINT = os.environ.get("FORGEHUB_ENDPOINT_NAME", "MiniMax H3")
WF_SYNC_DIR = Path(os.environ.get("WF_SYNC_DIR", "/tmp/forgehub-workflows"))
WF_SYNC_MANIFEST = WF_SYNC_DIR / ".manifest"

# Output delivery: base64 inline under these limits, S3 offload above them.
# RunPod's job-done callback drops results over ~20MB, so keep headroom.
INLINE_MAX = int(os.environ.get("OUTPUT_INLINE_MAX_MB", "10")) * 1024 * 1024
INLINE_BUDGET = int(os.environ.get("OUTPUT_INLINE_BUDGET_MB", "15")) * 1024 * 1024
S3_PREFIX = os.environ.get("S3_OUTPUT_PREFIX", "outputs")

# Config → unet filename needle + sampler values (mirrors chat_service.py _MINIMAX_CONFIGS)
MINIMAX_CONFIGS = {
    # PRISM (Singularity x 10Eros merge): 8 steps sharp, euler/beta, shift 12/3
    "prism_r2va_turbo":   {"unet": "PRISM_DMAD", "steps": 8, "sampler": "euler", "scheduler": "beta",   "shift_video": 12.0, "shift_audio": 3.0, "lora": None, "tau": None, "sigma_off": True},
    # 10Eros beta7 fix1: FL2VA base, 8-step turbo integrated, euler/simple, shift 12/3
    "10eros_fl2va_turbo": {"unet": "beta7_fix1",  "steps": 8, "sampler": "euler", "scheduler": "simple", "shift_video": 12.0, "shift_audio": 3.0, "lora": None, "tau": None},
    # Hybrid DMAD-PDMD: FL2VA backbone + Ref2VA guidance, 4-8 steps, CFG 1.0-1.5
    "hybrid_fl2va_r2va_turbo":  {"unet": "Hybrid-DMAD", "steps": 8, "sampler": "euler", "scheduler": "simple", "shift_video": 12.0, "shift_audio": 3.0, "lora": None, "tau": None},
}


def _http(path, payload=None, timeout=30):
    url = COMFY_URL + path
    try:
        if payload is None:
            return urllib.request.urlopen(url, timeout=timeout).read()
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=timeout).read()
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:2000]
        raise RuntimeError(f"ComfyUI {path} → HTTP {e.code}: {body}") from e


_BOOT_DONE = threading.Event()
_BOOT_ERR = None


def _boot_comfy():
    # Extra flags come from COMFYUI_EXTRA_ARGS (space-separated). Keep the
    # default empty: --fast/--use-sage-attention/--async-offload corrupt the
    # int8-convrot Qwen/MMh3 stacks (flat garbage output).
    cmd = [
        "python3", "main.py",
        "--listen", "127.0.0.1", "--port", "8188",
        "--disable-auto-launch",
        # triton kernels (kitchen) — safe on the int8-convrot stacks (NOT in the
        # corruption list, and the pods run these same models with it) — the
        # sage/fast/async-offload flags stay out, those corrupt the quantized
        # weights into flat garbage.
        "--enable-triton-backend",
    ] + os.environ.get("COMFYUI_EXTRA_ARGS", "").split()
    comfy_log = open("/workspace/comfy.log", "ab", buffering=0)
    proc = subprocess.Popen(cmd, cwd=COMFY_DIR, stdout=comfy_log, stderr=subprocess.STDOUT)
    deadline = time.time() + 600
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("ComfyUI exited during boot")
        try:
            _http("/system_stats", timeout=5)
            return proc
        except Exception:
            time.sleep(2)
    raise RuntimeError("ComfyUI did not become ready in 600s")


def _sync_workflows():
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{FH_REPO}/git/trees/{FH_BRANCH}?recursive=1",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "forgehub-worker"})
        tree = json.loads(urllib.request.urlopen(req, timeout=60).read())
        raw = lambda p: f"https://raw.githubusercontent.com/{FH_REPO}/{FH_BRANCH}/{p}"
        wanted = []
        for entry in sorted(tree.get("tree", []), key=lambda e: e["path"]):
            mp = entry["path"]
            if not (mp.startswith("workflows/") and mp.endswith(".meta.json")):
                continue
            meta = json.loads(urllib.request.urlopen(raw(mp), timeout=60).read())
            if meta.get("endpoint_name") == FH_ENDPOINT:
                wanted.append(mp[:-len(".meta.json")] + ".json")
        WF_SYNC_DIR.mkdir(parents=True, exist_ok=True)
        names = set()
        for wp in wanted:
            name = Path(wp).name
            tmp = WF_SYNC_DIR / f"{name}.tmp"
            tmp.write_bytes(urllib.request.urlopen(raw(wp), timeout=120).read())
            os.replace(tmp, WF_SYNC_DIR / name)
            names.add(name)
        if WF_SYNC_MANIFEST.is_file():
            for stale in set(WF_SYNC_MANIFEST.read_text().split()) - names:
                (WF_SYNC_DIR / stale).unlink(missing_ok=True)
        WF_SYNC_MANIFEST.write_text("\n".join(sorted(names)) + "\n")
        print(f"[handler] workflow sync: {sorted(names)}", flush=True)
    except Exception as exc:
        print(f"[handler] workflow sync failed: {exc} — using baked workflows", flush=True)


def _boot_in_bg():
    # ComfyUI boots on a daemon thread so the runpod handler is live (and the
    # `health` action answers) even while ComfyUI is still starting — or on
    # machines where it can't start at all (e.g. Hub test pods without the
    # network volume). Real jobs wait on _BOOT_DONE via _ensure_comfy().
    global _BOOT_ERR
    try:
        _sync_workflows()
        _boot_comfy()
    except Exception as exc:
        _BOOT_ERR = exc
        print(f"[handler] ComfyUI boot failed: {exc}", flush=True)
    finally:
        _BOOT_DONE.set()


async def _ensure_comfy():
    await asyncio.to_thread(_BOOT_DONE.wait)
    if _BOOT_ERR is not None:
        raise RuntimeError(f"ComfyUI failed to boot: {_BOOT_ERR} (see /workspace/comfy.log)")


def _media_bytes(value):
    if isinstance(value, str) and value.startswith("http"):
        return urllib.request.urlopen(value, timeout=120).read()
    return base64.b64decode(value)


def _upload(filename, data):
    boundary = uuid.uuid4().hex
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{filename}\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        COMFY_URL + "/upload/image", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    resp = json.loads(urllib.request.urlopen(req, timeout=300).read())
    return resp.get("name", filename)


def _load_workflow(name):
    """Workflow templates must be saved in ComfyUI *API format* (Export API),
    where every node is keyed by id and inputs carry widget names — the UI
    format's positional widgets_values can't be patched reliably."""
    dirs = [WF_SYNC_DIR, WORKFLOWS_DIR] if WF_SYNC_MANIFEST.is_file() else [WORKFLOWS_DIR]
    for base in dirs:
        path = base / name
        if not path.is_file():
            path = base / f"{name}.json"
        if path.is_file():
            break
    else:
        raise RuntimeError(f"workflow template not found: {name} (available: {sorted(p.name for d in dirs for p in d.glob('*.json'))})")
    wf = json.loads(path.read_text())
    if "nodes" in wf:
        raise RuntimeError(f"{name} is a UI-format graph; re-export it with 'Export API' so widgets are named")
    return wf


def _comfy_route(path, payload, timeout=300):
    """POST to a ComfyUI HTTP route and return the JSON body, or {} on any failure.

    Used for the Director's QwenVL routes (analyze/enhance) — they run inside the
    ComfyUI process this handler booted, so localhost is all it takes. A route that
    is missing (older image without the Director node) degrades to {} and the
    caller falls back to the raw text.
    """
    try:
        return json.loads(_http(path, payload, timeout=timeout))
    except Exception as exc:
        print(f"[handler] {path} failed: {exc}", flush=True)
        return {}


def _job_style_text(job):
    """The form's Video style option rides as a job field: the "[TAG] - " prefix
    strips off the same way the prompt-prepend path does."""
    style_raw = str(job.get("style") or "").strip()
    return re.sub(r"^\[[^\]]*\]\s*-\s*", "", style_raw) if style_raw and style_raw != "None" else ""


def _director_timeline(job):
    """Build the Director's timeline_data from the ForgeHub form, or None.

    The form gives the charsheet (images[0]), the segment ideas one per line in
    `prompt`, an optional style prose in `global_prompt`, and `seconds` split evenly
    across the ideas. The subject description comes from the form or is auto-generated
    by the Analyze route; each idea is then expanded by the Enhance route — the same
    built-in QwenVL the canvas buttons use — when `enhance` is on (default).

    The charsheet travels INSIDE the timeline (subjects[0].images as b64) — the
    Director's API graph has no LoadImage; that is how load_ref_image_tensors reads it.
    """
    ideas = [l.strip() for l in str(job.get("prompt") or "").splitlines() if l.strip()]
    if not ideas:
        return None

    char_b64 = ""
    images = job.get("images") or []
    if images:
        raw = _media_bytes(images[0])
        if raw:
            char_b64 = base64.b64encode(raw).decode()

    # The resolved style text opens the global prompt — the enhance's style
    # context and the compiled prompt's first lines carry it.
    global_prompt = "\n".join(x for x in (_job_style_text(job), str(job.get("global_prompt") or "")) if x.strip())

    seconds = int(job.get("seconds") or 10)
    per = max(1, round(seconds / len(ideas)))

    subject_desc = (job.get("subject_description") or "").strip()
    retention = ""
    if not subject_desc and char_b64:
        res = _comfy_route("/minimax_director/analyze_character", {
            "image_b64": char_b64, "char_index": 0, "kind": "person",
            "provider": "qwenvl", "base_url": "", "model": "",
        })
        if res.get("status") == "success":
            subject_desc = res.get("description") or ""
            retention = res.get("retention_note") or ""

    fps = 24
    segments = []
    cursor = 0
    for idea in ideas:
        text = idea
        if job.get("enhance", True) and char_b64:
            res = _comfy_route("/minimax_director/enhance_segment", {
                "image_b64": char_b64, "segment_text": idea,
                "global_prompt": global_prompt,
                "provider": "qwenvl", "base_url": "", "model": "",
            })
            if res.get("status") == "success" and (res.get("text") or "").strip():
                text = res["text"].strip()
        segments.append({
            "id": uuid.uuid4().hex, "start": cursor * fps, "length": per * fps,
            "prompt": text, "type": "text", "isEndFrame": False,
        })
        cursor += per

    total = cursor * fps
    ref_raw = str(job.get("reference_mode") or "REF2VA")
    ref_mode = "OFF" if ref_raw.strip().lower().startswith("off") else "REF2VA"
    tdata = {
        "mainTrackEnabled": True, "audioTrackEnabled": True, "motionTrackEnabled": True,
        "propHeight": 90, "globalPropHeight": 126,
        "showFilenames": False, "showPromptZones": True,
        "overrideAudio": False, "inpaint_audio": True,
        "global_prompt": global_prompt,
        "retake_global_prompt": "",
        "overall_soundscape": job.get("overall_soundscape") or "",
        "non_diegetic_music": job.get("non_diegetic_music") or "",
        "prompt_override": "", "prompt_override_on": False,
        "retakeMode": False, "retakeStart": 0, "retakeLength": 0,
        "retakePrompt": "", "retakeStrength": 1, "retakeVideo": None,
        "normalStartFrame": 0, "normalDurationFrames": total,
        "reference_mode": ref_mode, "prompt_format": "minimax",
        "analyzeProvider": "qwenvl", "analyzeBaseUrl": "", "analyzeModel": "",
        "summary": job.get("summary") or "", "task_type_override": "",
        "subjectSlotCount": 1,
        "subjects": [{
            "description": subject_desc, "retentionNote": retention,
            "images": ([{"b64": char_b64}] if char_b64 else []),
        }],
        "segments": segments, "motionSegments": [], "audioSegments": [],
    }
    return json.dumps(tdata), total


def _patch(prompt, job):
    director_nodes = [n for n in prompt.values()
                      if n.get("class_type") == "MiniMaxH3DirectorCS"]
    # Two Director flows: Native (the timeline owns the text — build it from the
    # form) and Storyboard (the in-graph QwenVL feeds external_prompt and the refs
    # ride the ref_images socket — the existing heuristics cover it; only the
    # window follows `seconds`). Injecting a timeline into the Storyboard would
    # double the references (subject b64 + socket), so it gets window-only.
    native_director = any(
        not isinstance(n.get("inputs", {}).get("external_prompt"), list)
        for n in director_nodes)
    director = _director_timeline(job) if native_director else None
    director_tdata, director_total = director if director else (None, 0)
    # The Director graphs carry their own two unets; the config preset's unet
    # needle would rewrite both to one model — keep the preset's sampler/LoRA
    # parts but blind the needle.
    cfg = MINIMAX_CONFIGS.get(job.get("config"))
    if director_nodes and cfg:
        cfg = {**cfg, "unet": None}
    base = re.sub(r"[^\w.-]+", "-", str(job.get("job_name") or f"job_{uuid.uuid4().hex[:8]}")).strip("-")
    images = [_upload(f"{base}_{i}.png", _media_bytes(v)) for i, v in enumerate(job.get("images") or [])]
    video_name = None
    if job.get("video"):
        video_name = _upload(f"{base}.mp4", _media_bytes(job["video"]))

    def _find_file(kind, needle):
        """Locate a model file under the volume + local model dirs."""
        hits = sorted(
            h for pat in (f"/runpod-volume/models/{kind}/**/*", f"{COMFY_DIR}/models/{kind}/**/*")
            for h in glob.glob(pat, recursive=True)
            if needle.lower() in Path(h).name.lower())
        # "fl2va"/"ref2va" needles must not grab 10Eros files which also contain them
        if needle.lower() in ("fl2va", "ref2va"):
            hits = [h for h in hits if "eros" not in Path(h).name.lower()] or hits
        return Path(hits[0]).name if hits else None

    img_idx = 0
    drop_lora = None
    sigma_off = {}
    for nid, node in prompt.items():
        ct = node.get("class_type", "")
        inp = node.get("inputs", {})

        if "LoadImage" in ct:
            if img_idx < len(images):
                inp["image"] = images[img_idx]
                if "LoadImageMiniState" in inp:
                    inp["LoadImageMiniState"] = images[img_idx]
                if "LoadImagePixState" in inp:
                    try:
                        state = json.loads(inp["LoadImagePixState"])
                        state["orig_name"] = images[img_idx]
                        inp["LoadImagePixState"] = json.dumps(state)
                    except Exception:
                        inp.pop("LoadImagePixState", None)
                img_idx += 1
        elif "LoadVideo" in ct and video_name:
            inp["video"] = video_name
        if ct == "MiniMaxH3DirectorCS" and director_nodes:
            # Native: inject the form-built timeline. Storyboard: leave the baked
            # one (its refs come from the socket). Either way the window follows
            # the timeline's total, or `seconds` when no timeline was built.
            if director_tdata is not None:
                inp["timeline_data"] = director_tdata
            total = director_total or (int(job["seconds"]) * 24 if job.get("seconds") else 0)
            if total:
                for wkey, val in (("start_frame", 0), ("end_frame", total),
                                  ("duration_frames", total),
                                  ("start_second", 0), ("duration_seconds", total / 24)):
                    if wkey in inp:
                        inp[wkey] = val
            if director_tdata is not None:
                continue
        if director_tdata is None and job.get("prompt") is not None and "prompt" in inp and not isinstance(inp["prompt"], list):
            inp["prompt"] = job["prompt"]
        if director_tdata is None and job.get("prompt") is not None and ct == "WildcardProcessor":
            inp["text"] = job["prompt"]
            inp["populated_text"] = job["prompt"]
        if director_tdata is None and job.get("camera_tag") and "camera_tag" in inp:
            inp["camera_tag"] = job["camera_tag"]
        if director_tdata is None and job.get("vl_preset"):
            for wkey in ("preset_prompt", "enhancement_style"):
                if wkey in inp:
                    inp[wkey] = job["vl_preset"]
        if director_tdata is None and "passthrough" in inp:
            inp["passthrough"] = job.get("enhance") is False
        # Storyboard: the QwenVL feeds external_prompt, so the style directive
        # cannot ride the global prompt — a StringConcatenate node prepends it
        # verbatim to the compiled prompt instead of letting the VLM weave it in.
        if director_nodes and director_tdata is None and ct == "StringConcatenate" \
                and "string_a" in inp and not isinstance(inp["string_a"], list):
            inp["string_a"] = _job_style_text(job)
        if director_tdata is None and job.get("seconds"):
            sec = int(job["seconds"])
            if "duration" in inp:
                inp["duration"] = f"{sec}s"
            elif "length" in inp and not isinstance(inp["length"], list):
                inp["length"] = sec * 24
            elif "value" in inp and ct.startswith("Primitive"):
                inp["value"] = sec
        if "lora_name" in inp:
            cur_lora = str(inp.get("lora_name", "")).lower()
            if "realism" in cur_lora or "character_swap" in cur_lora:
                # Style LoRA slot (Singularity recipe): job.lora =
                # none | realism | char_swap. Disabled/missing → drop the
                # node entirely below (ComfyUI validates lora_name even at
                # strength 0), so mark it for removal.
                sel = job.get("lora") or "none"
                drop_lora = nid
                if sel != "none":
                    needle = {"realism": "realism", "char_swap": "character_swap"}.get(sel, sel)
                    found = _find_file("loras", needle)
                    if not found:
                        raise RuntimeError(f"Style LoRA '{sel}' not found on the volume")
                    inp["lora_name"] = found
                    for skey in ("strength_model", "strength_clip"):
                        if skey in inp:
                            inp[skey] = 1.0
            elif cfg is not None:
                if cfg["lora"]:
                    found = _find_file("loras", cfg["lora"])
                    if found:
                        inp["lora_name"] = found
                        for skey in ("strength_model", "strength_clip"):
                            if skey in inp:
                                inp[skey] = 1.0
                else:
                    for skey in ("strength_model", "strength_clip"):
                        if skey in inp:
                            inp[skey] = 0.0
        elif cfg is not None and cfg.get("unet") and "unet_name" in inp:  # MiniMax H3 generator/enhancer node
            needle = cfg["unet"]
            if needle.lower() not in str(inp.get("unet_name", "")).lower():
                found = _find_file("unet", needle) or _find_file("diffusion_models", needle)
                if found:
                    inp["unet_name"] = found
            for wkey, ckey in (("steps", "steps"), ("sampler_name", "sampler"), ("scheduler", "scheduler"),
                               ("shift_video", "shift_video"), ("shift_audio", "shift_audio")):
                if wkey in inp and not isinstance(inp[wkey], list):
                    inp[wkey] = cfg[ckey]
            if cfg.get("tau") and "tau" in inp:
                inp["tau"] = cfg["tau"]
        if cfg is not None:
            # Sampler/step/shift values live on dedicated nodes, not on the
            # unet loader — patch them by class_type or the preset would only
            # swap weights/LoRA while the template settings silently win.
            if ct == "KSamplerSelect" and "sampler_name" in inp:
                inp["sampler_name"] = cfg["sampler"]
            elif ct == "BasicScheduler":
                for wkey in ("steps", "scheduler"):
                    if wkey in inp:
                        inp[wkey] = cfg[wkey]
            elif ct == "MiniMaxH3SigmaShift":
                if cfg.get("sigma_off"):
                    # API format ignores UI bypass modes — physically remove
                    # the node and rewire its consumers to its model source.
                    sigma_off[nid] = inp.get("model")
                else:
                    for wkey in ("shift_video", "shift_audio"):
                        if wkey in inp:
                            inp[wkey] = cfg[wkey]
    if sigma_off:
        for nid in sigma_off:
            del prompt[nid]
        for n in prompt.values():
            for k, v in n.get("inputs", {}).items():
                if isinstance(v, list) and v and v[0] in sigma_off:
                    n["inputs"][k] = sigma_off[v[0]]
    # LoadImage nodes left on the template filename (fewer uploads than loaders —
    # e.g. single-image edits with an optional image2) would fail validation;
    # drop them and unlink their consumers instead.
    uploaded = set(images)
    dead = [nid for nid, n in prompt.items()
            if "LoadImage" in n.get("class_type", "")
            and n.get("inputs", {}).get("image") not in uploaded]
    # Same for reference video loaders left on their template value when the
    # job carries no video (R2VA recipes have an optional ref_videos input).
    dead += [nid for nid, n in prompt.items()
             if "LoadVideo" in n.get("class_type", "")
             and n.get("inputs", {}).get("video") != video_name]
    if dead:
        while dead:
            for nid in dead:
                del prompt[nid]
            emptied = []
            for nid, n in prompt.items():
                for k in [k for k, v in n.get("inputs", {}).items()
                          if isinstance(v, list) and v and v[0] in dead]:
                    del n["inputs"][k]
                if not n["inputs"]:
                    emptied.append(nid)
            # Cascade: nodes left with zero inputs after unlinking (e.g. a
            # PixaromaImageInfo whose only input was a dropped loader) would
            # fail validation on required inputs — drop them too.
            dead = emptied
    if drop_lora is not None:
        # Rewire the dropped style LoRA's consumers to its model source.
        src = prompt[drop_lora].get("inputs", {}).get("model")
        del prompt[drop_lora]
        if isinstance(src, list):
            for n in prompt.values():
                for k, v in n.get("inputs", {}).items():
                    if isinstance(v, list) and v and v[0] == drop_lora:
                        n["inputs"][k] = src
    # Post-processing toggles: the graph chain is  save.images <- rife.frames
    # <- upscale.images <- VAEDecode.  upscale=false rewires rife.frames to the
    # decoder output; rife=false rewires save.images to rife's own source.
    upscaler = next((n for n in prompt.values() if "upscaler_trt_model" in n.get("inputs", {})), None)
    rife = next((n for n in prompt.values() if "rife_trt_model" in n.get("inputs", {})), None)
    saver = next((n for n in prompt.values()
                  if isinstance(n.get("inputs", {}).get("images"), list)
                  and n["inputs"]["images"]
                  and rife is not None
                  and n["inputs"]["images"][0] == next(k for k, v in prompt.items() if v is rife)), None) if rife else None
    if not job.get("upscale", True) and upscaler is not None and rife is not None:
        rife["inputs"]["frames"] = upscaler["inputs"]["images"]
    if not job.get("rife", True) and rife is not None and saver is not None:
        saver["inputs"]["images"] = rife["inputs"]["frames"]
        # The VHS saver bakes 48 (the RIFE'd rate) — halve it. The Director's
        # CreateVideo takes fps as a LINK from the Director's own frame_rate
        # output, which is already the pre-RIFE rate: leave it alone.
        if "frame_rate" in saver["inputs"] and not isinstance(saver["inputs"]["frame_rate"], list):
            saver["inputs"]["frame_rate"] = saver["inputs"]["frame_rate"] // int(rife["inputs"].get("multiplier", 2) or 2)
    # Upscaler model / target overrides — the loader node feeds "upscaler_trt_model".
    upscaler_loader = next((n for n in prompt.values()
                            if n.get("class_type", "").endswith("LoadUpscalerTensorrtModel")), None)
    if upscaler_loader is not None and job.get("upscale_model"):
        upscaler_loader["inputs"]["model"] = job["upscale_model"]
    if upscaler is not None and job.get("upscale_target"):
        upscaler["inputs"]["resize_to"] = job["upscale_target"]
    # Output size: "1344x768" rewrites the size/latent state and syncs the
    # Qwen encoder's reference resolution to the long edge. The new Pixaroma
    # helper nodes (Sizes/Dropdown) replace the old PixaromaResolution node.
    # Dropdowns feeding a TextEncodeQwenImage21 "resolution" input only —
    # other PixaromaDropdown nodes may control unrelated widgets.
    enc_ids = {nid for nid, n in prompt.items()
               if n.get("class_type", "").startswith("TextEncodeQwenImage21")}
    dropdown_ids = set()
    for nid in enc_ids:
        res = prompt[nid].get("inputs", {}).get("resolution")
        if isinstance(res, list) and res and str(res[0]) in prompt:
            dropdown_ids.add(str(res[0]))
    job_size_raw = str(job.get("video_size") or job.get("image_size") or "")
    if job_size_raw.strip().lower().startswith("auto"):
        # "auto": the PVR keeps its baked behavior (use_image_aspect drives it);
        # the Director's canvas derives from the first timeline image (0x0).
        for node in prompt.values():
            if node.get("class_type") == "MiniMaxH3DirectorCS":
                inp = node.setdefault("inputs", {})
                inp["custom_width"], inp["custom_height"] = 0, 0
    m = re.match(r"(\d+)\s*x\s*(\d+)", job_size_raw)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
        for nid, node in prompt.items():
            ct = node.get("class_type", "")
            inp = node.get("inputs", {})
            if ct == "PixaromaResolution":
                try:
                    state = json.loads(inp.get("ResolutionState") or "{}")
                except Exception:
                    state = {}
                state["w"], state["h"] = w, h
                inp["ResolutionState"] = json.dumps(state)
            elif ct == "PixaromaSizes":
                try:
                    state = json.loads(inp.get("SizesState") or "{}")
                except Exception:
                    state = {}
                state["w"], state["h"] = w, h
                inp["SizesState"] = json.dumps(state)
            elif ct == "EmptyLatentImagePresets":
                pw, ph = (w, h) if w >= h else (h, w)
                ratios = {
                    (1024, 1024): "1:1",
                    (1152, 896): "1.286:1",
                    (1216, 832): "1.46:1",
                    (1344, 768): "1.75:1",
                    (1536, 640): "2.4:1",
                }
                ratio = ratios.get((pw, ph))
                if ratio:
                    inp["dimensions"] = f"{pw} x {ph} ({ratio})"
                    inp["invert"] = h > w
            elif ct == "PixaromaDropdown" and str(nid) in dropdown_ids and inp.get("DropdownState"): 
                try:
                    state = json.loads(inp["DropdownState"])
                except Exception:
                    continue
                if state.get("type") == "int":
                    state["value"] = str(max(w, h))
                    inp["DropdownState"] = json.dumps(state)
            elif ct.startswith("TextEncodeQwenImage21") and isinstance(inp.get("resolution"), int):
                inp["resolution"] = max(w, h)
            elif ct == "MiniMaxH3DirectorCS":
                # the Director's own canvas: custom_width/height are its size widgets
                inp["custom_width"], inp["custom_height"] = w, h
            elif ct == "ComfyUI-PerfectVideoResolution":
                # explicit size beats the tier labels: _parse_resolution falls
                # through to _parse_size for a "WxH" value. use_image_aspect off —
                # a picked size is literal; "auto" never reaches this branch.
                inp["resolution"] = f"{w}x{h}"
                inp["use_image_aspect"] = False
    # Explicit encoder resolution (job["resolution"]) overrides the
    # image_size-derived value on the PixaromaDropdown / direct int input.
    res_override = job.get("resolution")
    if res_override:
        try:
            res_value = str(int(res_override))
        except (TypeError, ValueError):
            res_value = str(res_override)
        for nid in dropdown_ids:
            inp = prompt[nid].setdefault("inputs", {})
            try:
                state = json.loads(inp.get("DropdownState") or "{}")
            except Exception:
                continue
            state["value"] = res_value
            inp["DropdownState"] = json.dumps(state)
        for nid in enc_ids:
            inp = prompt[nid].setdefault("inputs", {})
            if isinstance(inp.get("resolution"), int):
                inp["resolution"] = int(res_value)
    # Per-job seed: baked seeds would repeat identical results across jobs.
    # An explicit job["seed"] pins every seed consumer to the same value.
    job_seed = job.get("seed")
    if job_seed is None:
        job_seed = random.randrange(0, 2**32)
    for node in prompt.values():
        inp = node.get("inputs", {})
        class_type = node.get("class_type", "")
        node_seed = job_seed % (2**32)
        if class_type == "PixaromaSeed":
            inp["seed"] = node_seed
            try:
                state = json.loads(inp.get("SeedState") or "{}")
            except Exception:
                state = {}
            state["runSeed"] = node_seed
            inp["SeedState"] = json.dumps(state)
            continue
        for skey in ("seed", "noise_seed"):
            if isinstance(inp.get(skey), int) and not isinstance(inp.get(skey), bool):
                inp[skey] = node_seed
    # %date:fmt% tokens are resolved by the ComfyUI frontend only for widget
    # values — an API-format prompt reaches the nodes literally (VHS then saves
    # the raw token into the filename and /view 404s). Expand here with the
    # same field codes as Pixaroma's _expand_date_tokens.
    now = time.localtime()
    fields = {
        "d": now.tm_mday, "dd": f"{now.tm_mday:02d}",
        "M": now.tm_mon, "MM": f"{now.tm_mon:02d}",
        "h": now.tm_hour, "hh": f"{now.tm_hour:02d}",
        "H": now.tm_hour, "HH": f"{now.tm_hour:02d}",
        "m": now.tm_min, "mm": f"{now.tm_min:02d}",
        "s": now.tm_sec, "ss": f"{now.tm_sec:02d}",
        "yy": f"{now.tm_year % 100:02d}", "yyyy": now.tm_year,
    }
    for n in prompt.values():
        for k, v in n.get("inputs", {}).items():
            if isinstance(v, str) and "%date:" in v:
                n["inputs"][k] = re.sub(
                    r"%date:([^%]+)%",
                    lambda m: "".join(str(fields.get(t, t))
                                      for t in re.findall(r"dd?|MM?|hh?|HH?|mm?|ss?|yyy?y?|.", m.group(1))),
                    v)
    return prompt


def _apply_node_params(prompt, params):
    """Apply generic {node_id:widget} patches, after the MMH3 presets."""
    for key, value in (params or {}).items():
        node_id, _, widget = key.rpartition(":")
        node = prompt.get(node_id)
        if isinstance(node, dict) and widget:
            node.setdefault("inputs", {})[widget] = value
    return prompt


def _interrupt_prompt(prompt_id):
    """Best-effort ComfyUI abort for a cancelled job: dequeue if pending,
    interrupt if mid-flight."""
    for path, payload in (("/queue", {"delete": [prompt_id]}), ("/interrupt", {})):
        try:
            _http(path, payload, timeout=5)
        except Exception as exc:
            print(f"[handler] {path} failed during cancel: {exc}", flush=True)


async def _queue_and_wait(prompt):
    # Async + awaited sleeps: the runpod SDK stops a job by cancelling its
    # task, so the CancelledError can only be delivered at an await — a sync
    # handler would freeze the event loop and never see the stop signal.
    client_id = uuid.uuid4().hex
    resp = json.loads(await asyncio.to_thread(
        _http, "/prompt", {"prompt": prompt, "client_id": client_id}, 60))
    prompt_id = resp["prompt_id"]
    deadline = time.time() + JOB_TIMEOUT_S
    try:
        while time.time() < deadline:
            hist = json.loads(await asyncio.to_thread(_http, f"/history/{prompt_id}", None, 30))
            entry = hist.get(prompt_id)
            if entry and entry.get("status", {}).get("status_str") == "error":
                msgs = [str(m) for m in entry.get("status", {}).get("messages", [])]
                raise RuntimeError(f"ComfyUI job failed: {' | '.join(msgs)[:800]}")
            if entry and entry.get("status", {}).get("completed"):
                return entry
            await asyncio.sleep(3)
    except asyncio.CancelledError:
        # Even if this await gets cancelled again, the orphan thread still
        # completes and ComfyUI gets interrupted.
        await asyncio.to_thread(_interrupt_prompt, prompt_id)
        raise
    raise TimeoutError(f"prompt {prompt_id} exceeded {JOB_TIMEOUT_S}s")


def _s3_cfg(job):
    cfg = job.get("s3Config") or (job.get("input") or {}).get("s3Config")
    if isinstance(cfg, dict) and cfg.get("endpointUrl") and cfg.get("accessId"):
        return cfg
    return None


def _s3_client(cfg):
    import boto3
    from botocore.config import Config
    host = urllib.parse.urlparse(cfg["endpointUrl"]).netloc
    region = cfg.get("region") or host.split(".", 1)[0].removeprefix("s3api-") or "us-east-1"
    return boto3.client(
        "s3",
        endpoint_url=cfg["endpointUrl"],
        aws_access_key_id=cfg["accessId"],
        aws_secret_access_key=cfg["accessSecret"],
        region_name=region,
        config=Config(s3={"addressing_style": "path"}),
    )


def _emit_file(filename, data, job, inline_used):
    """Small outputs inline as b64; large ones upload to the job's s3Config
    storage (e.g. the RunPod network volume S3 API) and return a presigned URL."""
    oversized = len(data) > INLINE_MAX or inline_used[0] + len(data) > INLINE_BUDGET
    s3c = _s3_cfg(job)
    if oversized and s3c:
        try:
            key = f"{S3_PREFIX}/{job.get('id', 'job')}/{filename}"
            client = _s3_client(s3c)
            client.put_object(Bucket=s3c["bucketName"], Key=key, Body=data)
            url = client.generate_presigned_url(
                "get_object",
                Params={"Bucket": s3c["bucketName"], "Key": key},
                ExpiresIn=int(os.environ.get("S3_URL_TTL_S", "3600")),
            )
            print(f"[handler] {filename} -> s3://{s3c['bucketName']}/{key} ({len(data)/1e6:.1f}MB)", flush=True)
            return {"filename": filename, "url": url}
        except Exception as e:
            print(f"[handler] s3 upload failed for {filename}: {e} — falling back to inline", flush=True)
    elif oversized:
        print(f"[handler] {filename}: {len(data)/1e6:.1f}MB over inline limit, no s3Config — result may be dropped by RunPod", flush=True)
    inline_used[0] += len(data)
    return {"filename": filename, "b64": base64.b64encode(data).decode()}


def _prune_volume_outputs(max_age_s=86400):
    """Drop stale volume output dirs — ForgeHub downloads right after the job."""
    root = Path("/runpod-volume") / S3_PREFIX
    cutoff = time.time() - max_age_s
    try:
        for d in root.iterdir():
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass


def _collect_new_files(since_ts, job, inline_used):
    """Fallback: anything written under ComfyUI's output dir since the job ran."""
    out_dir = Path(COMFY_DIR) / "output"
    try:
        files = sorted(p for p in out_dir.rglob("*") if p.is_file() and p.stat().st_mtime >= since_ts)
    except Exception:
        return []
    out = []
    for p in files:
        if p.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp", ".mp4", ".webm", ".gif", ".wav", ".mp3", ".flac"):
            continue
        out.append(_emit_file(p.name, p.read_bytes(), job, inline_used))
    return out


def _collect_outputs(history_entry, prompt, job, inline_used):
    out, texts = [], {}
    for node_id, node_out in (history_entry.get("outputs") or {}).items():
        print(f"[handler] output node {node_id}: keys={list(node_out.keys())}", flush=True)
        for key in ("images", "gifs", "videos"):
            for item in node_out.get(key, []) or []:
                params = urllib.parse.urlencode({
                    "filename": item["filename"],
                    "subfolder": item.get("subfolder", ""),
                    "type": item.get("type", "output"),
                })
                data = _http(f"/view?{params}", timeout=300)
                out.append(_emit_file(item["filename"], data, job, inline_used))
        src = (prompt or {}).get(node_id, {}).get("inputs", {}).get("source")
        if not isinstance(src, list):  # only ShowText-style nodes carry live text
            continue
        for t in node_out.get("text", []) or []:  # ShowText nodes → prompt trace
            src_ct = (prompt or {}).get(str(src[0]), {}).get("class_type", "")
            label = "Wildcards expanded" if "Wildcard" in src_ct else "Final prompt (QwenVL)"
            texts[label] = t if isinstance(t, str) else str(t)
    return out, texts


async def handler(job):
    print(f"[handler] job {job.get('id')} received", flush=True)
    inputs = job.get("input") or {}
    if inputs.get("action") == "health":
        # Hub smoke test: answers 200 without touching ComfyUI or models, so it
        # also works on an endpoint deployed without the network volume.
        return {
            "status": "ok",
            "comfyui": "failed" if _BOOT_ERR else ("ready" if _BOOT_DONE.is_set() else "booting"),
            "comfyui_error": str(_BOOT_ERR) if _BOOT_ERR else None,
            "workflows": sorted({p.name for d in (WF_SYNC_DIR, WORKFLOWS_DIR) for p in d.glob("*.json")}),
            "volume_mounted": os.path.isdir("/runpod-volume"),
        }
    # Any real workload needs ComfyUI — wait for the background boot first.
    await _ensure_comfy()
    _prune_volume_outputs()
    # _patch uploads input media (blocking, up to minutes for video) — run it
    # off-thread so a cancel signal can still be delivered during uploads.
    if isinstance(inputs.get("prompt_graph"), dict):
        # Raw API-format graph shipped inside the job (ForgeHub mode): the same
        # _patch applies — media upload, config preset, unet/lora needle, prompt.
        prompt = await asyncio.to_thread(_patch, inputs["prompt_graph"], inputs)
    else:
        wf = _load_workflow(inputs.get("workflow") or "")
        prompt = await asyncio.to_thread(_patch, wf, inputs)
    prompt = _apply_node_params(prompt, inputs.get("params"))
    print(f"[handler] queuing prompt ({len(prompt)} nodes)", flush=True)
    started = time.time()
    comfy_log = Path("/workspace/comfy.log")
    log_mark = comfy_log.stat().st_size if comfy_log.is_file() else 0
    history = await _queue_and_wait(prompt)
    print(f"[handler] comfy execution took {time.time() - started:.1f}s", flush=True)
    inline_used = [0]
    outputs, texts = await asyncio.to_thread(_collect_outputs, history, prompt, job, inline_used)
    try:  # surface ComfyUI warnings/errors from this job in the worker log
        with open(comfy_log, "rb") as f:
            f.seek(log_mark)
            interesting = [l for l in f.read().decode(errors="replace").splitlines()
                           if re.search(r"warn|error|missing|unexpected|fail|dtype|clip|traceback", l, re.I)]
            for l in interesting[-40:]:
                print(f"[comfy] {l}", flush=True)
    except Exception:
        pass
    if not outputs:
        outputs = _collect_new_files(started, job, inline_used)
        print(f"[handler] fallback sweep — {len(outputs)} file(s) in output/", flush=True)
    print(f"[handler] done — {len(outputs)} output(s)", flush=True)
    return {"outputs": outputs, "texts": texts}


if __name__ == "__main__":
    threading.Thread(target=_boot_in_bg, daemon=True).start()
    runpod.serverless.start({"handler": handler})
