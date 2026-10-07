![MiniMax H3 Turbo — Video Generation](https://raw.githubusercontent.com/huchukato/runpod-minimax-h3/main/media/banner.png)

# MiniMax H3 Turbo — ComfyUI Serverless Worker

[![Runpod](https://api.runpod.io/badge/huchukato/runpod-minimax-h3)](https://console.runpod.io/hub)

A production-ready [Runpod Serverless](https://docs.runpod.io/serverless/overview) worker running **MiniMax H3** (Comfy-Org INT8 ConvRot) on ComfyUI — text/image/first-last-frame/reference-to-video **with audio**, lightx2v 8-step turbo LoRAs, TensorRT RIFE interpolation + upscaling, wildcard prompts and an uncensored Qwen3-VL enhancer.

Built for [ForgeHub](https://github.com/huchukato/ForgeHub), a desktop + self-hosted frontend that drives these workflows — but the endpoint speaks plain Runpod API, so anything can call it.

## Get the app

Deployed the endpoint? Drive it with ForgeHub — workflows, wildcard browsing, job queue and outputs included. Paste your API key and it detects the endpoint by itself.

[![Download ForgeHub](https://raw.githubusercontent.com/huchukato/runpod-minimax-h3/main/media/forgehub-release.jpeg)](https://github.com/huchukato/ForgeHub/releases/latest)

## Workflows included

| Workflow | Description |
| --- | --- |
| `MiniMaxH3-Turbo-T2VA.json` | Text → video + audio |
| `MiniMaxH3-Turbo-I2VA.json` | Image → video + audio |
| `MiniMaxH3-Turbo-FL2VA.json` | First/last frame → video + audio |
| `MiniMaxH3-Turbo-R2VA.json` | Reference images → video + audio |
| `MiniMaxH3-Singularity-R2VA.json` | Singularity fusion — up to 3 ref images + ref video → video + audio |

## Requirements

- **GPU**: 96GB class (`BLACKWELL_96` pool — e.g. B200). The 32B NVFP4 text encoder + video DiTs + TensorRT engines need the headroom.
- **Network volume**: ≥160GB attached to the endpoint at `/runpod-volume`. Workers auto-download missing models from `models-manifest.txt` (~100GB — first cold start is long; subsequent workers reuse the volume). TensorRT engines also compile once onto the volume. Disable with `VOLUME_AUTOPOPULATE=false` if you manage the volume yourself.
- **CUDA**: 13.0+ hosts.

## Model stack (auto-provisioned)

- DiTs: Comfy-Org `minimax_h3_fl2va` INT8, `Minimax-h3_Singularity_ref2va` INT8 (T2V/I2V/R2V/V2V in one unet), `10Eros_Max_h3_hybrid_beta5` INT8
- Text encoder: `qwen3vl_32b_heretic_minimax_h3_nvfp4` (Momoking)
- Turbo LoRAs: lightx2v fl2v/ref2v 8-step, TenStrip fusion turbo
- Style LoRAs (Singularity `lora` param): `h3-realism-people` (fal), `h3_character_swap` (akatz)
- VAEs: MiniMax H3 video fp16 + audio fp32, `taeh3` preview VAE
- Face detailer models: `face_yolov8m` + `sam_vit_b` (Easy-Use/Impact)
- Enhancer LLM: Qwen3.5-9B Defiant-Fable NEO-MAX Q6_K GGUF + mmproj

## Job input

```json
{
  "input": {
    "workflow": "MiniMaxH3-Turbo-FL2VA.json",
    "prompt": "[DOLLY IN] a __pmp/prmpt/lctns__ rooftop at dusk",
    "config": "native_turbo",
    "camera_tag": "[ORBIT]",
    "seconds": 6,
    "images": ["https://…", "https://…"],
    "upscale": true,
    "rife": true
  }
}
```

- `config` presets: `native` / `native_turbo` / `r2va_singularity` / `r2va_singularity_turbo` / `10eros` / `10eros_turbo` — they swap the DiT needle, turbo LoRA, steps/sampler/scheduler and shift values
- `lora` — style LoRA on the Singularity recipe: `none` (default) / `realism` / `char_swap`
- `images` — up to 3 reference images (1 image ≈ I2V behavior); `video` — URL or base64, fills the reference-video loader (R2VA)
- `upscale` / `rife` — toggle the TensorRT post-processing chain
- `"action": "health"` — returns `{"status": "ok", "workflows": […], "volume_mounted": bool}` without running a generation

## Custom nodes baked in

`ComfyUI-VideoHelperSuite` · `ComfyUI-Easy-Use` · `ComfyUI-Impact-Pack` · `ComfyUI-Impact-Subpack` · `ComfyUI-QwenVL-Mod` · `ComfyUI-RIFE-TensorRT-Auto` · `ComfyUI-Upscaler-TensorRT-Auto` · `ComfyUI-TagForge` · `ComfyUI-PerfectVideoResolution` · `ComfyUI-Pixaroma` · `ComfyUI-KJNodes`

## Companion stack

- 🖼 Image sibling: [huchukato/runpod-qwen21](https://github.com/huchukato/runpod-qwen21) (Qwen Image 2.1 T2I/Edit + Pony)
