# Changelog

## v1.0.0

Lazy ComfyUI boot + restrict Hub test to CUDA 13.x hosts
hub.json: listing title
MiniMax H3 Turbo serverless worker — initial Hub release


## v1.0.1

_(no changes)_


## v1.0.2

readme: absolute image urls so they render on the Hub listing
Add missing LoraLoaderModelOnly to R2VA/T2VA/FL2VA templates; r2va_native uses ref_lora; install libgl1/libglib for VHS cv2 import
Move ForgeHub CTA under the intro
Drop redundant ForgeHub link from companion stack (CTA banner below)
Replace ForgeHub CTA with slim 3.6:1 banner
Slim ForgeHub CTA banner (960x404, 103KB)
Cap ForgeHub CTA banner at 640px
Add ForgeHub download CTA banner
Fix missing threading import breaking worker startup


## v1.0.3

Singularity R2VA stack + README update


## v1.0.4

handler: drop disabled/missing style LoRA node and dead LoadVideo; bypass ModelPreviewOverrideKJ in R2VA workflows


## v1.0.5

add Singularity launch banners, new README hero


## v1.0.6

readme: fix storage link (console.runpod.io/user/storage)
readme: step-by-step deploy (network volume + max workers)
hub.json: drop endpointType LB so only Serverless endpoint deploy is offered


## v1.0.7

fix config presets never applying + official turbo sampler + timing


## v1.0.8

Sync with serverless repo: TagForge v2.3.0 pin, dynmc wildcard, Singularity handler presets, VHS save_metadata wf
Manifest: garage detectors + TRT engines (non-Sharp upscaler)


## v1.0.9

S3 offload for large outputs (s3Config -> presigned URL, ~20MB gateway cap) + boto3
Rebuild: pull QwenVL-Mod e186a98 (comfy dialogue syntax, <d>[Lang], soundscape fix)
Fix Face Detailer chain in entrypoint


## v1.0.10

Hub test: RTX 4090 -> RTX 5090 (4090 capacity exhausted, MIG fallback fails fitness check)


## v1.0.11

_(no changes)_


## v1.0.13

Split model manifests per endpoint; drop 10Eros/TenStrip/pmpIncaseStyle/extra upscalers
Rebuild: pull QwenVL-Mod 5615fae (Garage preset sync, ensure_minimax_music/dialogue, R2VA id=r2va + --NAME-- markers)


## v1.0.14

Disable xet in volume populate: chunk cache overflows 30GB container disk (EDQUOT)


## v1.0.16

handler: cascade dead-input cleanup — nodes left with zero inputs after loader removal (PixaromaImageInfo) get dropped too; enables true T2VA (0 images) on Singularity workflow
fix empty-prompt path: patch prompt widget whenever key present (empty clears baked prompt), empty baked prompt in Singularity-R2VA like Turbo workflows — restores image-only I2VA behavior
Wildcards: drop removed vid/act.yaml, add mmh3/* set (camera/music/nsfw/style/vfx)
Rebuild: pull QwenVL-Mod 21a279b (deterministic section vars, leaked-tag resolver, paraphrased VO wrap)


## v1.0.17

_(no changes)_


## v1.0.18

_(no changes)_


## v1.0.19

handler: boot-sync workflows from ForgeHub + fix %date%/urlencode output lookup
Dockerfile: drop baked wildcard wget — wildcards ship via TagForge boot-sync from Garage


## v1.0.20

baked fallback: the two Director API workflows (with the TensorRT post chain) — the boot-sync covers v1.0.20, these land in the next image build
rife=false toggle: don't divide a linked fps — the Director's CreateVideo takes frame_rate as a link already carrying the pre-RIFE rate
Director in ForgeHub: _director_timeline builds the timeline from the form (ideas one per line, auto-Analyze, per-segment Enhance via the in-process QwenVL routes) — Native flow injects it, Storyboard flow keeps its baked timeline and only follows seconds; config preset skipped on Director graphs (two unets); Dockerfile: MiniMaxH3-Director node


## v1.0.21

image_size patches the Director's canvas too (custom_width/height) — the resolution selector for the Director workflows


## v1.1.0

hub test: back to the 5090 — the Hub's test deployer rejects MIG slice ids (INVALID_INPUT) and the 5090 is the only CUDA-13-capable type with stock right now
hub test: the smoke test's GPU is a MIG 24GB slice — the health action touches no models, and the 5090/A40/A6000 pools are in a supply drought (the v1.0.21 test burned on deployment, not on the image)
revert: the easy fullLoader branch was in the wrong handler — Pony jobs run on the qwen21 endpoint, the branch belongs there
image_size: the easy fullLoader branch — the Pony workflow's resolution picker (space-format SDXL buckets + latent w/h)
video_size: the resolution job field the mmh3 metas send (image_size kept as fallback — the RecipeForm's SizePicker owns that key for the qwen21 workflows)
image_size auto: the Director's canvas derives from the first timeline image (custom 0x0) — the PVR keeps its baked use_image_aspect behavior
image_size: explicit sizes drive the PVR node too (resolution=WxH parses as an explicit size, use_image_aspect off) — the Director-style resolution selector for the Singularity workflow


## v1.1.1

remove the Hub smoke test entirely — every test deployment burned on the GPU supply drought; the image is validated by the live endpoint's health check instead
skip the Hub smoke test: rename tests.json -> tests_.json — the validator allocates the test GPU at deploy time and the supply drought burned two releases in a row; the image content is proven by the v1.0.20 health check on the live endpoint. Reversible by renaming back when the supply returns.


## v1.1.2

the Video style dropdown composes into the built timeline's global prompt (the enhance's style context + the compiled prompt's opening) — the ideas stay segments
baked fallback: the Director Native with the turbo sampler baked
the References switch reaches the built timeline (REF2VA / OFF) — OFF runs the fl2va fallback path text-only
baked fallback: the Director Native with the LoRA slots + the compiled-prompt ShowText
config preset on Director graphs: keep the sampler/LoRA parts, blind the unet needle (the graph carries its own two unets — the turbo preset now drives the ref2v_turbo LoRA slot and the 6-step euler/beta sampler)
README: the Director workflows (Native multi-segment + Storyboard single idea), the Director job fields, video_size resolution control and the Director node in the baked list


## v1.1.3

Storyboard style prepend via StringConcatenate + handler writes the directive into string_a
baked fallback: the Director Storyboard with the turbo sampler + the LoRA slots


## v1.1.4

tests.json: align with ltx-25 (4090, no autopopulate env)
restore Hub smoke test — RunPod may require tests.json for indexing now
handler: passthrough follows the enhance flag in both directions
Hub title: MiniMax H3 - Video & Audio Generation
README: quick start now says Max workers = 1 (Runpod deploy default is 3; one warm worker with queued jobs beats N cold starts on this stack)
Director node: renamed repo ComfyUI-MiniMaxH3-Director-Enhanced in Dockerfile clone + README node list


## v1.1.5

boot: --enable-triton-backend (safe on int8-convrot, pods run it)
hybrid config: _turbo suffix (distills fused in)
configs: user-facing presets prism_r2va_turbo / 10eros_fl2va_turbo / hybrid_fl2va_r2va with per-model sampling
serverless mmh3: PRISM (R2VA) + 10Eros beta7 (FL2VA), no turbo LoRAs, no taeh3, no native FL2VA/v1.3; Storyboard wf removed, workflows rewired
drop Director-Storyboard from workflow list


## v1.1.6

drop baked workflows-api (ForgeHub sync is the only source)
chore: update models-manifest.txt with correct LoRA style URLs
chore: update hub.json to v1.1.6 with changelog and manifest cleanup

