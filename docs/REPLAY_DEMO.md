# Static kuruLab replay demo

Branch: `deploy/replay-demo`. Deployable root: `replay-demo/`.

This is a static recording viewer. It has no simulation endpoint, historical
download, wallet connection, Python service, Node service, or API credentials.
The full local application remains available from the repository root.

## Vercel import settings

| Setting | Exact value |
| --- | --- |
| Plan | Personal Hobby project |
| Framework preset | Other |
| Root directory | `replay-demo` |
| Build command | Override enabled, field empty |
| Install command | Override enabled, field empty |
| Output directory | `.` |
| Environment variables | None |
| Production branch | `deploy/replay-demo` |
| Files outside the root directory | Excluded |

`replay-demo/vercel.json` sets the framework, empty build and install commands,
output directory, and response headers. There are no Functions, Cron Jobs,
databases, or paid integrations. Static hosting remains subject to Hobby usage
limits. See [Vercel static build settings](https://vercel.com/docs/builds/configure-a-build)
and [Hobby plan](https://vercel.com/docs/plans/hobby).

## Deploy this branch

1. Push the tested demo branch to GitHub:

   ```bash
   git push -u origin deploy/replay-demo
   ```

   Do not merge it into `main` as a requirement for this demo.
2. In Vercel, use a personal Hobby account. Select **Add New → Project** and
   import `Jackkrzyz/kuru-lp-backtester`.
3. Enter the import settings above. Use the branch selector if it is available.
   Do not add environment variables or enable files outside `replay-demo`.
4. Set the project's production branch to `deploy/replay-demo` under the Git
   settings. The production branch must contain `replay-demo`; `main` does not
   need to contain these files.
5. If the initial import builds the default branch, its deployment can fail
   because `replay-demo` is absent there. After setting the branch and root,
   open **Deployments → Create Deployment** and enter `deploy/replay-demo`, or
   `https://github.com/Jackkrzyz/kuru-lp-backtester/tree/deploy/replay-demo`.
   Select **Create Deployment**. This uses the chosen branch, not `main`.
6. Open the resulting HTTPS URL. Check the welcome modal, each recording,
   playback controls, mobile layout, and console. In the Network panel, only
   static asset and `data/*.json` requests should appear. There must be no
   `/api/` requests. Check that the response headers match `vercel.json`.
7. Record the public URL and tested commit before calling the demo deployed.

Vercel documents [deployments from a Git reference](https://vercel.com/docs/git)
and [custom production branches](https://vercel.com/kb/guide/can-i-use-a-non-default-branch-for-production).

No public URL has been deployed or tested during this implementation.

## Actual exported recordings

All three use `kuru-lp-contract-v18`. Hashes were checked against the saved
completion markers before export. The default is the unflagged Spot recording.

| Run ID | Shape | Blocks, inclusive | Frames | Fills | Net P&L, USDC | Material impact |
| --- | --- | --- | ---: | ---: | ---: | --- |
| `lp-3dcbac7069fbb06a80b8` | Spot | 109132028–109142027 | 879 | 3 | -1.7249738019380478 | No |
| `lp-2d41d3f8be336bf585c6` | Bid-Ask | 109097028–109107027 | 1,237 | 5 | 8.225090183399153 | No |
| `lp-c7e25b4ab5e0a3abf683` | Curve | 109092057–109107978 | 1,500 | 313 | 25376.11838890465 | Yes, 100% |

The Curve recording is a very large position. Its saved warning is displayed
beside its results and in the selection label. Its P&L is not presented as a
reliable live-profit estimate. These are recorded examples, not tuned strategy
recommendations. The selected files include exact saved configurations,
intervals, metrics, impact records, initial grids, and unchanged compact frames.
Displayed metrics are rounded; JSON retains the saved numbers.

`data/catalog.json` contains file SHA256 hashes, original deterministic summary
and timeline hashes, saved-summary and detailed-fill hashes, and verification
report/event/checkpoint hashes. It contains no local paths or job tokens.
The demo's `.gitattributes` preserves exact JSON bytes across Git checkouts.

The initial static bundle is about 3.55 MB. Each recording is fetched only when
selected with **Load Replay**, then retained in browser memory. There is no
background polling. Playback reads the loaded frames only.

## Export and verify locally

These are development commands. Python and Node are not required by the hosted
demo. The committed data and browser assets are ready to serve without a build.

```bash
python tools/export_replay_demo.py
python -m pytest -q tests/test_replay_demo.py
node sdk_reference/test_replay_demo.js
python -m http.server 8005 --bind 127.0.0.1 --directory replay-demo
```

Open `http://127.0.0.1:8005/`. Stop the temporary static server with Ctrl+C.
Do not use the full backtest server to host the public demo.

The exporter reads only local saved runs and the local verified historical
index. It checks the saved summary file, deterministic summary, timeline,
detailed fills, final cash/inventory/P&L, frame/fill counts, event partitions,
passing validation reports, and starting checkpoint. It stops if any selected
recording fails. It does not generate another backtest or substitute a result.
Export requires the original local files; a clone can serve and test the
committed demo without those original files.

To select different completed records explicitly:

```bash
python tools/export_replay_demo.py --run-id lp-3dcbac7069fbb06a80b8 \
  --run-id lp-2d41d3f8be336bf585c6 --run-id lp-c7e25b4ab5e0a3abf683
```

Keep the selection small. Each exported recording must use the current engine,
have at most 1,500 frames, and be smaller than 3 MB. Do not accept an export
failure as a completed release. If no valid local recordings are available,
obtain valid saved runs locally before exporting.

## Frontend reuse and assumptions

The exporter copies the existing base stylesheet and the current Canvas price,
portfolio, active-order, and playback functions from `frontend/app.js` into
`assets/replay-renderer.js`. The full frontend is not edited. It does not copy
the form, API handlers, SDK worker, download controls, or server code.

`demo.js` adds static-file loading, SHA256 checks, read-only configuration,
visible load errors, accessible playback state, and the welcome dialog. Tab
and Shift+Tab cycle between the dialog buttons. Escape dismisses it. The
**Recorded replay demo** header label and GitHub link remain visible. Gas
exclusions and unchanged historical taker behavior are visible in the page.

Changing the recording clears the previous result. Missing or corrupt files
produce an error; they never show a placeholder or an old result. Playback,
pause, 1x/10x/50x/100x speed selection, and scrubbing use the saved timeline.
Speed labels retain the local app's frame-based playback rates; they are not
a guarantee of exact historical wall-clock acceleration.

## Verification

- Five release tests check exported hashes and financial state, renderer/source
  reuse, private-file exclusions, failed exports, and the real static controller.
- The controller test loads all three actual recordings and checks configuration,
  metrics, play/pause, speed, forward/backward scrubbing, and a visible HTTP 404.
  Its requests are restricted to static `data/*.json` files.
- The full suite passed **386 tests and 14 subtests** in **93.47 seconds**.
- Browser checks loaded all three recordings with no console errors or warnings.
  The modal passed Tab, Shift+Tab, Escape, and keyboard dismissal checks.
- Desktop and mobile checks passed. At 390 px, the page and Canvas charts had
  no horizontal overflow; the modal was 341 × 568 px inside a 390 × 844 px
  viewport. Its controls remained reachable. The exact configured CSP was also
  tested locally with no console warnings or errors during valid replay loads.
- A deliberate missing-file browser test showed HTTP 404 in the page and hid
  results. The actual recording was not removed. The normal static server
  recorded 36 requests to nine distinct static paths and zero `/api/` requests.
  Local screenshots are under `.cache/ui/static-*`; they are not deployed.
  Temporary test servers are not part of this deployment.

All tracked full-application files remain unchanged on this branch. New files
are limited to the static directory, exporter, tests, and this guide. Export
and test evidence does not establish a public deployment.
