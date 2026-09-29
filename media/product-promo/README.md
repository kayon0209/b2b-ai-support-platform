# B2B Support product film

26-second Chinese product overview for the repository README. It uses the real customer SupportChat, operator Workbench, and approval ProposalPanel with deterministic synthetic data. The navy film wrapper and original music are created in this project. See `BRIEF.md`, `DIRECTION.md`, `evidence/feature-evidence.md`, and `evidence/component-usage.json` for scope and traceability.

The scenes show desktop web only. The order, customer, approval and case receipt are synthetic; this film does not demonstrate enterprise ERP/CRM connectivity, model accuracy, or production readiness.

## Rebuild

Requires Node 22+, Python 3.12+, FFmpeg and Playwright Chromium headless shell. From this directory:

```sh
npm ci
npx playwright install chromium --only-shell
python3 score.py
python3 tools/mix_audio.py plan.json
npm run build
node render.mjs --audio assets/master.wav --output renders/final.mp4
python3 tools/check_delivery.py plan.json --video renders/final.mp4 --mix-report evidence/audio-mix.json
node stills.mjs evidence/stills 1.5 6.5 12 18 24
node cover.mjs --scale 1
```

The repository README uses the published `docs/media/product-promo.mp4` and `docs/media/product-promo-cover.webp`. The cover source is `cover/cover.html` and a frame rendered from this film. The published MP4 is H.264/AAC, 1920×1080, 30 fps and 26 seconds. Source code adaptations, fixture API and display dependencies are isolated here.

`LICENSE` preserves the skill starter's AGPL-3.0 license. Original SFX files come from the skill's `assets/audio/sfx/` with provenance in `evidence/audio-selection.json`. No CodePilot fallback components or third-party music are used.
