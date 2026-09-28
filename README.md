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
  row's **⋯** menu copies the request into a new one or deletes it. Click a row
  for its detail page: progress, video, prompt, and reference images.
- **Tạo request**: title (defaults to the prompt's first line), the prompt for
  node 190, three reference images for LoadImage nodes 28, 29, and 30, and
  duration (2/5/10/30 s), MP4 size, and resolution (0.2 MP preview or the
  original 0.4 MP / 1 MP stages). Defaults: 5 s, compact, 0.2 MP.
- **Cài đặt**: save, test, or delete the Runpod API key. A saved key is required
  to submit, and lets the server keep downloading finished videos after the
  browser is closed.

## Hosting on another machine (private, over Tailscale)

The server only listens on `127.0.0.1`. `tailscale serve` publishes it as
`https://<machine>.<tailnet>.ts.net` to devices in your tailnet only. Funnel is
never enabled, so nothing is reachable from the internet or the local network.

Requirements on the host: macOS or Linux, `git`, and Tailscale signed in to your
tailnet with MagicDNS and HTTPS Certificates enabled
(<https://login.tailscale.com/admin/dns>). The install script installs Python
3.10+ if needed (Homebrew on macOS, apt/dnf on Linux); the app has no other
dependencies.

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
`.deploy.env`, so later deploys only need `scripts/deploy.sh -m "..."`. Use the
host's Tailscale name so SSH also stays inside the tailnet (Tailscale SSH or
regular SSH both work). `--no-remote` only pushes.

## Data and security

Everything is stored on the host under `data/` (ignored by Git):

| Item | Location |
|---|---|
| Title, prompt, settings, full workflow, job ID, status, errors | `data/studio.db` (SQLite) |
| Reference images and returned MP4 | `data/requests/<request id>/` |
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

Each job has a 20-minute execution timeout and a 1-hour TTL. A request is
limited to 9.5 MB, and the worker returns MP4s larger than 8 MB only when its S3
output settings are configured.

## Running locally without a service

```bash
python3 web_ui.py            # http://127.0.0.1:8765
python3 web_ui.py --port 8766 --allow-host studio-host.tail1234.ts.net
```

`H3_DATA_DIR` moves the data folder; `H3_PORT` and `H3_ALLOWED_HOSTS`
(comma-separated) are alternatives to the flags.
