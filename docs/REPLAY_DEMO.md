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
completion markers before export. The page waits for a shape selection.
Each example uses the same blocks, a symmetric ±3% range, 0.5% spread,
10,000 MON, and 350 USDC. The MON amount drives the SDK grid.

| Run ID | Shape | Blocks, inclusive | Frames | Fills | Net P&L, USDC | Material impact |
| --- | --- | --- | ---: | ---: | ---: | --- |
| `lp-995fc312c0bda392b4d2` | Spot | 109097028–109107027 | 1,244 | 10 | 4.28277063963589 | Yes, 100% |
| `lp-6c78bf41c3fd2180bbfd` | Curve | 109097028–109107027 | 1,244 | 10 | 3.83311385926048 | Yes, 100% |
| `lp-a726314e55a343920cd7` | Bid-Ask | 109097028–109107027 | 1,244 | 10 | 4.60431119042318 | Yes, 100% |

All three retain their saved transaction-impact warnings beside the replay.
Their P&L is not presented as a reliable live-profit estimate.
These are basic shape examples, not tuned strategy recommendations.
The selected files include exact saved configurations,
intervals, metrics, impact records, initial grids, and unchanged compact frames.
Displayed metrics are rounded; JSON retains the saved numbers.

`data/catalog.json` contains file SHA256 hashes, original deterministic summary
and timeline hashes, saved-summary and detailed-fill hashes, and verification
report/event/checkpoint hashes. It contains no local paths or job tokens.
The demo's `.gitattributes` preserves exact JSON bytes across Git checkouts.

The static bundle is less than 7 MB, including the unchanged main-app PNG
favicon (about 3.02 MB). The three recordings are checked and retained in
browser memory on page load. Each selection window shows its saved SDK grid
with quote-notional bar heights and the basic configuration.
The selected grid stays visible for 1.1 seconds before replay starts at 100×.
The complete replay window stays hidden until the user selects a shape.
Closing the welcome dialog does not select a shape or start playback.
The shape windows fade out in 400 ms. The replay fades in over 700 ms while
playback runs. The outgoing scene cannot receive keyboard or pointer input.
**Pick another shape** stops playback and returns to the windows with a fade.
The page then waits for another selection, including a repeat of the same shape.
Reduced-motion settings remove the transition effects.
There is no background polling. Playback reads the loaded frames only.

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
python tools/export_replay_demo.py --run-id lp-995fc312c0bda392b4d2 \
  --run-id lp-6c78bf41c3fd2180bbfd --run-id lp-a726314e55a343920cd7
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
a guarantee of exact historical wall-clock acceleration. The demo accumulates
elapsed time to show 100 frames per second at 100× on screens with lower refresh
rates. It renders the final frame before stopping. A shape change, the About
dialog, or a hidden page cancels pending automatic playback.

## Verification — 2026-10-14

- Five release tests check exported hashes and financial state, renderer/source
  reuse, private-file exclusions, failed exports, and the real static controller.
- The controller test loads all three actual recordings and checks exact grids,
  explicit selection, both scene fades, the return control, automatic 100×
  playback, selection changes, configuration, metrics,
  play/pause, speed, forward/backward scrubbing, and a visible HTTP 404.
  Its requests are restricted to static `data/*.json` files.
- All five focused release tests passed. These checks include the static
  controller test with all three new recordings.
- Saved financial state, timeline hashes, file hashes, and grid funding passed.
- Market quotes retain their own price axis. Recorded executions use separate
  buy/sell time lanes and an exact-price list.
- These checks do not establish a public deployment.
