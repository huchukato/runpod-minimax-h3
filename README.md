![MiniMax H3 Singularity — one model to rule them all](https://raw.githubusercontent.com/huchukato/runpod-minimax-h3/main/media/banner-singularity.jpg)

# MiniMax H3 Turbo — ComfyUI Serverless Worker

[![Runpod](https://api.runpod.io/badge/huchukato/runpod-minimax-h3)](https://console.runpod.io/hub)

A production-ready [Runpod Serverless](https://docs.runpod.io/serverless/overview) worker running **MiniMax H3** (Comfy-Org INT8 ConvRot) on ComfyUI — text/image/first-last-frame/reference-to-video **with audio**, multi-scene **storyboards via the Director**, turbo LoRAs, TensorRT RIFE + upscaling, wildcards and an uncensored Qwen3-VL enhancer. Built for [ForgeHub](https://github.com/huchukato/ForgeHub), but the endpoint speaks plain Runpod API.

## Deploy

1. **Storage first**: create a [network volume](https://console.runpod.io/user/storage) (≥160GB) in a datacenter with 96GB Pro GPUs — e.g. `US-NE-1`.
2. Click **Deploy** → endpoint → **Edit** → set **Max workers = 1** + attach your volume. Cold start is heavy (~100GB of models on first boot, big model loads after), so one warm worker with jobs queued beats N cold workers — Runpod's deploy default is 3, lower it. Workers auto-download the models on first boot (once). No volume = every job fails on missing models.
3. Call it via the Runpod API or ForgeHub below — workflows, wildcards, job queue and outputs included; paste your API key and it detects the endpoint by itself.

[![Download ForgeHub](https://raw.githubusercontent.com/huchukato/runpod-minimax-h3/main/media/forgehub-release.jpeg)](https://github.com/huchukato/ForgeHub/releases/latest)

## Workflows included

| Workflow | Description |
| --- | --- |
| `MiniMaxH3-Turbo-T2VA.json` | Text → video + audio |
| `MiniMaxH3-Turbo-I2VA.json` | Image → video + audio |
| `MiniMaxH3-Turbo-FL2VA.json` | First/last frame → video + audio |
| `MiniMaxH3-Turbo-R2VA.json` | Reference images → video + audio |
| `MiniMaxH3-Singularity-R2VA.json` | Singularity fusion — up to 3 ref images + ref video |
| `MiniMaxH3-Director-Native.json` | **Director**: character sheet + one idea per line → multi-scene storyboard with audio; the worker auto-analyzes the subject and expands each idea via the built-in QwenVL |
| `MiniMaxH3-Director-Storyboard.json` | **Director**: one idea (+ up to 3 storyboard frames) → single video; the in-graph QwenVL turns the idea into a full R2VA prompt |

## Requirements

- **GPU**: 96GB class (`BLACKWELL_96` pool). The 32B text encoder + two video DiTs + TensorRT engines need the headroom.
- **Network volume**: ≥160GB at `/runpod-volume`. Models auto-download from `models-manifest.txt` (~100GB). Disable with `VOLUME_AUTOPOPULATE=false`.
- **CUDA**: 13.0+ hosts.

## Model stack (auto-provisioned)

- DiTs: Comfy-Org `minimax_h3_fl2va` INT8, `Minimax-h3_Singularity_ref2va` INT8 (T2V/I2V/R2V/V2V in one unet)
- Text encoder: `qwen3vl_32b_heretic_minimax_h3_nvfp4` (Momoking)
- Turbo LoRAs: lightx2v fl2v/ref2v 8-step · Style LoRAs: `h3-realism-people` (fal), `h3_character_swap` (akatz)
- VAEs: video fp16 + audio fp32, `taeh3` preview · Face detailer: `face_yolov8m` + `sam_vit_b`
- Enhancer LLM: Qwen3.5-9B Defiant-Fable NEO-MAX Q6_K GGUF + mmproj (also drives the Director's auto-Analyze/Enhance)

## Job input

```json
{
  "input": {
    "workflow": "MiniMaxH3-Director-Native.json",
    "prompt": "she turns from the window, hair catching the golden light\nshe walks toward the camera, playful smile",
    "global_prompt": "warm cinematic realism; her look stays the same in every shot",
    "seconds": 10,
    "images": ["<character sheet>"],
    "enhance": true,
    "video_size": "1344x768 (16:9)",
    "upscale": true,
    "rife": true
  }
}
```

- **Director Native**: `prompt` = one idea per line (each = a segment, `seconds` split evenly); `global_prompt` = style prose; `subject_description` optional (auto-Analyze when empty); `enhance` = per-segment QwenVL autoprompting (default true)
- **Director Storyboard**: `prompt` = one idea (timestamps welcome); `images` = up to 3 storyboard frames; `vl_preset` = `MiniMax › Director Storyboard` / `Director Prose`
- Classic: `config` presets (`native`/`native_turbo`/`r2va_singularity`/`r2va_singularity_turbo`/`10eros`/`10eros_turbo`) swap DiT/LoRA/steps/sampler/shift; `lora` = style LoRA on Singularity (`none`/`realism`/`char_swap`)
- `video_size` — `auto` (follows the image/stills) or an explicit MMH3 native preset (`1344x768 (16:9)`, `1184x864 (4:3)`, …)
- `upscale` / `rife` — TensorRT post chain · `"action": "health"` — status + workflow list, no generation

## Custom nodes baked in

`ComfyUI-VideoHelperSuite` · `ComfyUI-Easy-Use` · `ComfyUI-Impact-Pack` · `ComfyUI-Impact-Subpack` · `ComfyUI-QwenVL-Mod` · `ComfyUI-MiniMaxH3-Director-Enhanced` · `ComfyUI-RIFE-TensorRT-Auto` · `ComfyUI-Upscaler-TensorRT-Auto` · `ComfyUI-TagForge` · `ComfyUI-PerfectVideoResolution` · `ComfyUI-Pixaroma` · `ComfyUI-KJNodes`

## Companion stack

- 🖼 Image sibling: [huchukato/runpod-qwen21](https://github.com/huchukato/runpod-qwen21) (Qwen Image 2.1 T2I/Edit + Pony)
