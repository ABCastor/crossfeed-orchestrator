Load for image generation/editing or audio/video understanding and comparison.

# Media transports

Verify transport against the installed script interfaces. Revalidate after CLI/model changes; advertised modalities and old arena scores do not prove current delivery or quality.

| Job | Transport | Boundary |
|---|---|---|
| Image generation/editing with Codex tools | Official Codex image_gen on its login, attach refs where supported | Copy outputs into the working root; verify actual images |
| Scripted Gemini image generation/editing | `scripts/gemini-image.sh --prompt TEXT --out FILE [--ref IMAGE ...]` | Metered, separate reservation ledger and daily cap |
| Audio/video | `scripts/gemini-media.sh --file FILE --modality audio|video --prompt TEXT` | Native Files API, only admitted model, separate ledger |
| Independent image reading | Admitted OpenCode `--file`/`--modality image` | Route metadata alone is not proof |

Codex supports reference images in the recorded CLI transport; do not repeat the obsolete fresh-only limitation. Gemini --ref is repeatable. Choose based on required reference count, tools, scriptability and current evidence, rather than hard-coded arena ranks. Model switches apply to image/media gates too. A confirmed generation result and actual copied output are acceptance evidence; a description of what could be generated is not.

Codex: `codex exec --skip-git-repo-check [-i ref.png ...] < prompt.txt`. Originals land in `~/.codex/generated_images/`; copy them into the working root. Flatten alpha onto white (`sips -s format jpeg in.png --out flat.jpg`) before judging: an alpha cutout can look near-black. Prompt it to try once and report, not to describe. Keep the sandbox boundary in [isolation](isolation.md); this recipe grants no sandbox bypass.

Gemini image defaults to its script's admitted Pro image model, with --cheap selecting the admitted Flash image route. Both are paid: explicit authorization and daily budget apply. It reserves before calls and retains ambiguous failures, so retries cannot quietly reset spend. Gemini media admits only gemini-flash-lite-latest, uploads and counts tokens, then reserves full output allowance before generateContent. Its default wall budget is 600 seconds and default cap $1/local calendar day. Unknown MIME, upload/key/timeout/budget errors stop the run; interrupted requests retain reservations for the day.

Image, native media and fleet gemini-metered accounting are separate mechanisms, not a shared atomic budget. Do not claim unified cap protection. Raising an environment cap needs the owner's instruction. Paid Google OpenCode routes use start-of-run estimated-spend admission, so one in-flight call can overshoot; provider budgets/request caps supply the billing backstop.

Audio/video do not go through agy (no file flag) or the currently unproved OpenCode binary-file path. swarm media-review routes audio/video to native Gemini and image to the admitted OpenCode profile. Retest source capabilities only when delivered transport changes.

For design, diagnose a render before generating replacements, critique with a different model, and verify alpha/transparency and readability after rendering. Put prompt craft in the design/image skill (use your own design or image-generation guidance), not in this routing engine. Do not promote historical quality scores to current defaults or copy private design payload into a free/public lane.
