# Hosting and hackathon submission plan

Date: 11 October 2026. Status: proposed; nothing was deployed or purchased.

## Recommendation

Prepare a repository link, a short recorded demo, and a reproducible validated
data bundle first. Add a hosted judge app if there is time to pass the public
access checks. Do not depend on a repository link alone until the organizer
confirms the submission fields and eligibility rules.

The [official Metropolis portal](https://hackathon.monad.xyz/) displays
1 September to 13 October. Its form requires sign-in. This review could not
verify the exact cutoff time, timezone, or required submission fields. Check
those in the user's registered account. The public partner page has conflicting
date labels, so it is not used to set a deadline here.

The user supplied the rule that the demo must run on Monad Mainnet or Testnet.
This product uses historical data from Kuru's official MON-USDC Mainnet market,
listed in [Kuru's contract directory](https://docs.kuru.io/contracts/Contract-addresses).
It is an off-chain backtester, not a deployed strategy. Ask the organizer:
“Does a read-only app that reconstructs and validates Monad Mainnet contract
events satisfy this rule?” Do not claim that a web deployment or wallet button
alone establishes eligibility. Do not rush a funded transaction to meet a rule.

## Smallest hosted version

Use one Linux host with persistent storage and a supported production HTTP
service. Keep the current Python engine and pinned Node SDK. Separate short
HTTP requests from bounded worker jobs. Put TLS and rate limits at the public
entry point. Do not publish the current local `http.server` directly; Python
documents its [production-use limitation](https://docs.python.org/3/library/http.server.html).

```text
Browser -> HTTPS API -> bounded job queue -> Python worker + pinned Node SDK
                           |                          |
                    status/result store       read-only validated history

Private indexing process -> validation -> immutable dataset promotion
```

Start with blocks **109092028–109142027** only. Additional downloaded blocks
must not appear as backtestable until validation and promotion pass. Keep
indexing separate from judge requests. Do not give public users download,
stop-download, arbitrary RPC, or filesystem controls.

This choice follows the create-a-backend skill: classify request work, worker
work, and persistent data before choosing a host. Vercel is an option for a
frontend/API, but the current local subprocess and filesystem design needs
adaptation, not a direct static-site upload. Review current
[Function limits](https://vercel.com/docs/functions/limitations) before choosing
that route. A split deployment adds queue, storage, authentication, and origin
work; it is not the first choice for this deadline.

## Deployment steps and gates

1. Fix the job-state and public-access findings in the
   [code review](../reviews/2026-10-11_REVIEW.md). Keep engine rules unchanged.
2. Package the source revision, Python dependencies, Node lockfile, frontend,
   and SDK bundle. A Python wheel alone does not contain all app assets.
3. Export a release data bundle with the event partitions, checkpoints,
   contract rules, validation reports, and a hash manifest needed by the app.
   Exclude `.env`, credentials, logs, unrelated downloads, and private results.
   Check redistribution rights. Keep large data outside Git source history.
4. Test bundle import and one real run in a clean environment with no Envio
   token. Require all three golden results and fail-closed coverage checks.
5. Mount immutable history read-only. Use a separate writable result volume.
   Set job, order-count, duration, memory, and disk limits. Start with one worker;
   measure the largest permitted run before allowing more concurrent work.
6. Add user/job ownership, request limits, a public-origin allowlist, secure
   cookies, CSRF checks, and private admin access. Remove secrets from errors
   and logs. Test the actual HTTPS path, not only localhost.
7. Test refresh, canceled jobs, worker failure, host restart, full disk, result
   recovery, and two simultaneous users. Preserve result hashes across hosts.
8. Add a health check, error log, disk alert, backup, and rollback procedure.
   Set a spending limit before choosing a paid plan. No cost estimate is
   promised until a host, storage size, and allowed run size are selected.

## Submission package

- Repository URL and exact commit. Explain SDK and contract versions.
- A short demo: choose a covered interval, preview a position, run it, inspect
  fills and hold comparison, then show Envio download flow and validation.
- Clearly distinguish live download progress, validated historical data, and
  simulated orders. If a replay is prerecorded, label it.
- A validation table with parity counts, zero reconstruction errors, golden
  hashes, and known limits. Do not claim guaranteed future profit or fills.
- A clean-clone setup guide and the hash-checked sample bundle. A clone alone
  currently lacks ignored local history; judges cannot run it from source alone.
- Hosted URL if ready. Otherwise state local-only and supply the demo and setup
  instructions, subject to organizer approval. Check all prize-track fields,
  required disclosures, licenses, and video limits in the signed-in portal.

Wallet execution is not required to operate this historical backtester. Its
separate plan must not delay a complete, honest submission of the current app.
