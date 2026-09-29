# MiniMax H3 Studio

A small private admin for sending MiniMax H3 Cinematic jobs to the Runpod
serverless endpoint `xjh0wwhkto13ky` and keeping every request, reference image,
and generated video. The worker image itself lives in
[minimax-h3-cinematic-runpod](https://github.com/Shaney17/minimax-h3-cinematic-runpod);
this repository is only the web studio, so changing it never triggers a Runpod
rebuild.

## Screens

- **Requests** (home): every saved request, newest first. The filter matches
  titles regardless of case and Vietnamese accents ("rung" finds "Rừng"). Each
  row's **⋯** menu continues the shot, copies the request into a new one, or
  deletes it. Click a row for its detail page: progress, video, prompt,
  reference images, and the scene chain. Continued shots are marked with ↳.
- **Tạo request**: title (defaults to the prompt's first line), the prompt for
  node 190, three reference images for LoadImage nodes 28, 29, and 30, and
  duration, MP4 size, and resolution (0.2 MP preview or the original
  0.4 MP / 1 MP stages). Defaults: 5 s, compact, 0.2 MP. Durations are 3 s (a
  smoke test) and 5, 8, 10, 12, 15 s: MiniMax H3 is trained on 124–362 frames
  (about 5–15 s at 24 fps), so longer clips are unsupported and easily exceed
  the time limit. For a longer story, render several ≤15 s shots. The form shows
  the last measured run time for the chosen duration and resolution.
- **Cài đặt**: save, test, or delete the Runpod API key. A saved key is required
  to submit, and lets the server keep downloading finished videos after the
  browser is closed.

## Continuing a shot (frame anchor)

Each request is independent, so consecutive shots do not join by themselves.
**Tiếp nối cảnh** (on a finished request's page or in the row's ⋯ menu) starts
the next shot from the exact last frame of the previous video:

1. It copies the prompt, settings, and the three subject images, and takes the
   video's exact final frame with ffmpeg on the host
   (`GET /api/requests/<id>/last-frame`).
2. The frame is sent as a fourth image, `<Picture 4>`. The request adds three
   nodes to the workflow: `LoadImage` 300, and `MiniMaxH3AddGuide` 301/302 that
   anchor it at frame 0 of stage 1 (base latent, node 121) and stage 2 (upscaled
   latent, node 157). Each stage needs its own guide because the node encodes
   the image at the size of the latent it receives.
3. It adds the wording from MiniMax's official Ref2VA prompt guide:
   `<Picture 4> is the first frame of [Shot 1]` in `subject_definitions`,
   `keyframe completion` in the `summary` task type, and a `retention_analysis`
   line. Rewrite the scene description for the new shot, and keep
   `subject_definitions` unchanged between shots so the characters stay stable.

The form's step 04 does the same manually: pick a finished request or upload any
image, then **Thêm câu neo vào prompt**. A request with the anchor off sends the
original workflow unchanged. Anchored shots are linked; a shot's page lists its
chain and **Phát liên tục** plays the stored scenes back to back.

The anchor needs no worker rebuild: the nodes ship with ComfyUI v0.37 in the
current image. Whether this community hybrid FL2VA/Ref2VA checkpoint honours
references and a keyframe together as well as the official Ref2VA model does is
still to be measured.

## Long films (Phim dài)

**Phim dài** turns a list of scenes into one video without keeping a browser
open. The user splits the story; the server does the rest:

1. Enter the three reference images, the shared `subject_definitions`, a style
   sentence, default ambience and music, resolution and MP4 size, then add
   scenes (3–15 s each, 5 s by default) with an English description and optional
   per-scene sound.
2. **Bắt đầu tạo phim** renders the scenes one after another. For each scene the
   server builds the six-field Ref2VA prompt (shared definitions, a summary,
   retention lines, style + description, sound); from scene 2 on it adds the
   `<Picture 4>` anchor wording and anchors frame 0 to the previous scene's last
   frame (ffmpeg). The scene description should open in that same state.
3. A failed scene is submitted once more; a second failure pauses the film with
   the error, and **Thử lại** continues from that scene. **Dừng** pauses after the
   current job (which still finishes on Runpod).
4. When all scenes are done, ffmpeg joins them into `film.mp4`, dropping the
   repeated first frame of every scene after the first.

Finished and running scenes are locked. Later scenes can be edited, reordered,
or added while the film is paused, failed, or finished; adding scenes to a
finished film renders only the new ones and joins the film again. Each scene is
also a normal request in **Requests**, and its page shows the exact prompt sent.

Rendering is sequential because each scene needs the previous video, so a film
takes about the sum of its scenes' run times plus queueing.

## Hosting on another machine (private, over Tailscale)

The server only listens on `127.0.0.1`. `tailscale serve` publishes it as
`https://<machine>.<tailnet>.ts.net` to devices in your tailnet only. Funnel is
never enabled, so nothing is reachable from the internet or the local network.

Requirements on the host: macOS or Linux, `git`, and Tailscale signed in to your
tailnet with MagicDNS and HTTPS Certificates enabled
(<https://login.tailscale.com/admin/dns>). The install script installs Python
3.10+ and ffmpeg if needed (Homebrew on macOS, apt/dnf on Linux); the app has
no other dependencies. The service gets ffmpeg's directory on its `PATH`, since
launchd and systemd start services with a minimal one.

First install, on the host:

```bash
git clone https://github.com/Shaney17/minimax-h3-studio.git ~/minimax-h3-studio
~/minimax-h3-studio/scripts/install.sh
```

The script:

1. pulls the latest code and checks the workflow;
2. installs a background service that starts at login/boot and restarts on
   failure — a launchd agent on macOS (`~/Library/LaunchAgents`), a systemd user
   service with lingering on Linux;
3. runs `tailscale serve --bg --https=443 http://127.0.0.1:8765`;
4. prints the tailnet URL.

Run it again at any time to update. Options: `--port N`, `--no-pull`,
`--no-serve` (skip Tailscale and use the host only locally).

On macOS the launchd agent runs while your user is logged in. For an unattended
Mac, enable automatic login or keep the user session open.

## Deploying from your development machine

```bash
scripts/deploy.sh --host you@studio-host -m "Describe the change"
```

This checks the code (Python, JavaScript, shell syntax, and the workflow — also
against the worker `Dockerfile` when `../minimax-h3-cinematic-runpod` or
`H3_WORKER_DIR` exists), commits all changes, pushes `main` to GitHub, then SSHs
to the host to pull and rerun `install.sh`. The host is remembered in
`.deploy.env`, so later deploys only need `scripts/deploy.sh -m "..."`. Add
`--dir ~/path/to/checkout` when the host keeps the repo somewhere other than
`~/minimax-h3-studio`; it is remembered too. Use the
host's Tailscale name so SSH also stays inside the tailnet (Tailscale SSH or
regular SSH both work). `--no-remote` only pushes.

## Data and security

Everything is stored on the host under `data/` (ignored by Git):

| Item | Location |
|---|---|
| Title, prompt, settings, full workflow, job ID, status, errors | `data/studio.db` (SQLite) |
| Reference images, returned MP4, and cached last frame | `data/requests/<request id>/` |
| Film reference images and the joined `film.mp4` | `data/projects/<film id>/` |
| Runpod API key | `data/studio.db`, created with owner-only permissions |

Every device in your tailnet can open the studio and use the saved key; there is
no separate login. Keep the tailnet to your own devices, or restrict the host
with Tailscale ACLs. Requests whose `Host` or `Origin` is not localhost or the
host's tailnet name are rejected. Delete `data/` to remove all saved data.

## Workflow

`workflow/cinematic_api.json` is the API graph sent with every job, so it must
match the models and custom nodes baked into the worker image. After changing
the worker, copy its workflow files here and run:

```bash
python3 scripts/check_workflow.py --worker-dir ../minimax-h3-cinematic-runpod
```

The server sets each job's Runpod policy: a 60-minute execution timeout and a
2-hour TTL (queueing and cold starts included). A per-job policy overrides the
endpoint's own timeout, so keep `H3_EXECUTION_TIMEOUT_MS` equal to the endpoint
setting if you change it. Runpod's queue and run times are stored for every job. A request is
limited to 9.5 MB, and the worker returns MP4s larger than 8 MB only when its S3
output settings are configured.

## Running locally without a service

```bash
python3 web_ui.py            # http://127.0.0.1:8765
python3 web_ui.py --port 8766 --allow-host studio-host.tail1234.ts.net
```

`H3_DATA_DIR` moves the data folder; `H3_PORT` and `H3_ALLOWED_HOSTS`
(comma-separated) are alternatives to the flags.
