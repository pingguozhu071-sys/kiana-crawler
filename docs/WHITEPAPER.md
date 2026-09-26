# kiana-crawler — Technical Whitepaper (v2.19.8)

**Design and Implementation of a Single-Machine Web Collection Engine for Windows**

Repository: <https://github.com/pingguozhu071-sys/kiana-crawler>
Document type: technical whitepaper, written for engineers evaluating the design and implementation quality of this project
Companion documents: [README](../README.md) (four languages) · [Code provenance & license statement](来源与合规声明.md) · [Image asset provenance](ASSET-PROVENANCE.md)

---

## Abstract

Modern websites defend automated collection on three planes — TLS fingerprinting at the transport layer, JavaScript challenges at the execution layer, and behavioral rate-and-trajectory detection at the session layer — while media platforms additionally seal their downloadable streams behind signed, time-limited payloads embedded in page state. This whitepaper describes how kiana-crawler answers those obstacles under a deliberately narrow product envelope: one person, one Windows machine, no server. The engine resolves reachability with a five-tier fallback scheduler, concurrency correctness with a lease-and-compare-and-swap protocol, egress security with a hop-by-hop SSRF gate, data hygiene with three redaction layers and explicitly written exclusions, and long-run stability with a two-level token bucket and a per-page watchdog. Quality is enforced by an offline test line of 675 cases and a ten-check release gate. Every quantitative claim in this document is traceable to the repository and is indexed in Appendix A.

**Keywords**: web collection; anti-bot resistance; concurrency correctness; SSRF; redaction; resumable download; release engineering

---

## 1. Positioning: what this is, and what it is not

This document is not marketing copy. Each number below — module counts, line counts, test counts, timeouts, thresholds — resolves to a concrete source: a source file, a constant, or a repository document. Any claim can be re-verified with one `grep` or one `pytest --collect-only` run. Where evidence is weaker or a question remains open, the document says so and labels the item accordingly.

The product envelope is a **single-machine** tool: not a framework, not a service, not multi-tenant. This is a product decision, and it buys three things — zero deployment, zero server maintenance, and data that never leaves the machine. The envelope also defines the refusals, which are stated up front rather than buried: the tool does not cross account permissions (login state comes only from cookies the user supplies; when they are insufficient it reports a readable error), it does not enable large-model features by default, and it does not let personal data reach the disk unredacted.

---

## 2. The problem space

Four failure modes dominate single-machine collection:

| # | Failure mode | Concrete shape |
|---|---|---|
| 1 | **Unreachable content** | TLS-fingerprint gates reject non-browser clients; JS challenges filter non-executing requests; behavioral detection throttles high-frequency access. No single strategy covers all three. |
| 2 | **Concurrent corruption** | Duplicate processing of one page, results overwritten by racing workers, interleaved database writes — all silent. |
| 3 | **Dirty output** | Duplicates, empty shells, third-party phone numbers and emails, session tokens inside URLs. Redaction itself has boundaries: applied in the wrong place it breaks the pipeline. |
| 4 | **Liveness collapse** | One hung page stalls a batch; a naive rate limiter turns retries into a never-terminating storm. |

Media platforms add a fifth: playable URLs live inside embedded page JSON, signed and expiring, so a generic downloader fed the page URL routinely misses the best quality.

---

## 3. System shape

### 3.1 Composition

| Component | Scale |
|---|---|
| Engine (`kiana_vnext_plus/`) | 67 Python modules · 22,635 lines |
| Desktop UI (`launcher_v9.py`) | 1,645 lines · five-page FluentWindow (PySide6 + qfluentwidgets) |
| Tests | 675 cases — 674 pass / 1 skip / 0 fail — fully offline |
| Dependencies | 25 runtime + 6 dev, all pinned with `==` |
| Site rules | 12 declarative YAML files, each required to ship a real-page sample |
| Queue storage | SQLite · 8 tables · idempotent migrations via `PRAGMA user_version` (schema v5) |
| Deliverable | Two exes (PyInstaller onedir) + NSIS multilingual installer · measured ≈542 MB |

The installer size is a deliberate trade: Chromium, ffmpeg, the PO-Token Node service and the deno runtime all ship inside the package, so a fresh install runs with zero runtime downloads. `docs/BUILD.md` records the ≈1.8 GB installed footprint.

### 3.2 Process model: the engine is not a subprocess

`EngineBridge` runs the asyncio event loop on a daemon thread inside the GUI process and calls the engine entry `crawl()` in-process; `print` and `logging` are bridged into Qt signals. The payoff is direct state access, one-process debugging, and no inter-process protocol to package. The price is a hard rule — **the GUI never touches engine objects directly**, only the bridge's stop/pause/resume controls and a read-only reference — and a cross-thread discipline: returning to the main thread uses `QApplication.postEvent`, not direct signal emission. Both rules exist because their violation caused real incidents.

Exit codes are a fixed contract: 0 completed, 1 stopped by user, 2 engine crash, 3 engine load failure.

### 3.3 The life of one page (17 stages)

Establish (1–3): task directory (`cli_<sha8>/` holding the queue database, export tree, `stats.jsonl` contract feed, `checkpoint.json`, logs) → seed routing → enqueue. Execute (4–12): claim batch under lease → pick exit node and concurrency slot → heartbeat → cache probe → five-tier routing → parse-bomb guard → redact → deduplicate → quality gate. Emit (13–17): CAS claim → persist/export → media enqueue → link discovery → statistics.

---

## 4. Mechanisms, as layers of defense

### 4.1 Reachability layer — five-tier fallback

`engine_router.py` orders the tiers by cost: protocol channel (curl_cffi TLS impersonation) → stealth channel (source-level anti-detection) → TLS switch → real browser (patchright primary, playwright fallback) → direct connection. A failure or a challenge promotes the request; while the cheap tier succeeds, the browser process is never launched — which is why rendering additionally carries its own budget cap, preventing "browser storms" under concurrency.

Two details are load-bearing. **Interception and escalation use different signals**: a gated request returns 400, while 403 is reserved as the "escalate to the browser tier" signal — conflating them sends blocked URLs into paid browser renders. **The stealth channel refuses JS injection**: runtime-property spoofing leaves detectable traces, so the engine relies on protocol-level fixes and launch-argument hygiene (patchright's Runtime.Enable leak repair), with an explicit distinction between stealth removal and UX removal.

### 4.2 Consistency layer — lease and CAS

The frontier queue (`frontier.py`) is eight SQLite tables migrated idempotently to `user_version` 5. Correctness rests on two primitives. **Lease**: claiming writes `leased_at`/`lease_expires`, renewed by a 60-second heartbeat that renews `lease_expires` — never `leased_at`, whose mutation would break the CAS loop. **CAS**: persistence requires winning a compare-and-swap with tri-state semantics — won (write), lost (write nothing), and *database failure* (`None`, which must raise and retry; treating `None` as "lost" would silently swallow a locked database). Failure paths carry the same rigor: `mark_failed` updates carry `AND status != 'done'`, because a watchdog once returned completed pages to retry and caused full re-crawls with duplicate exports — now a regression case.

The standing rule against races: **a read-modify-write across `await` is a real race; occupy the slot synchronously before awaiting**. The robots gate and the render budget are both built this way.

### 4.3 Flow-control layer — two-level token bucket, terminable retries

Rate limiting is a global-plus-per-domain two-level token bucket, but its decisive property is the **separation of retry semantics**: being throttled does not consume retry budget. Throttling increments a dedicated counter that dead-letters after repeated occurrences; only genuine transport failures increment `retry_count`. Without this separation, a throttled task lives in the retry queue forever and the main loop's exit condition never fires — the job would not run slowly, it would never finish. `Retry-After` is honored but capped at 300 s so one server cannot park a batch for hours. Concurrency runs 20–50 coroutines globally and 20 per domain; the three-level arbitration takes the minimum, and stop/resume controllers must return their claims and recompute — a leftover low value otherwise pins concurrency permanently.

### 4.4 Egress security layer — hop-by-hop SSRF

The most expensive lesson in the repository: **entry-point URL validation is insufficient**. An external image URL can answer 302 toward an internal address, and the HTTP library follows it — reproduced locally with two loopback services, with the internal response saved to disk as an ordinary image. Consequently every outbound fetch passes through `url_utils.py`, the most-imported module in the engine:

- entry validation, automatic redirects disabled, **manual hop-by-hop re-validation** (curl_cffi follows up to 30 redirects by default and its response object has no `.url` attribute, so re-validation can only be manual);
- hostname normalization before judgement — IPv6 zone IDs and trailing-dot FQDNs both once slipped past the blocklist;
- **25 private-address forms**, including decimal, hex and octal IP notations, covering ranges the standard library misses;
- **DNS verdicts expiring after 90 s**, defeating resolve-public-then-repoint-internal rebinding;
- a **10-hop redirect cap**.

One exception is documented rather than hidden: `llm_client` intentionally carries no gate, because its endpoints are user-supplied local inference services (e.g. Ollama at `http://localhost:11434`) that a gate would break. Security mechanisms may have justified exceptions, but the justification lives in the comments.

### 4.5 Data hygiene layer — three redaction layers with written exclusions

Redaction covers three leak surfaces. **Logs**: the filter attaches to handlers — Python logging invokes ancestors' handlers but not ancestors' logger filters, so attaching to the root logger is a no-op; new handlers receive redaction automatically through a wrapped `logging.Handler.__init__`. **Side channels**: the print bridge, the GUI log path and exception echoes each pass through redaction. **Artifacts**: exported copies are scrubbed record-wide by `sanitize_record`, recursing six levels deep. Nineteen sensitive URL-parameter classes (token/key/sign/session/csrf and kin) plus phone, email and IP patterns are covered; the HTTP cache stores sanitized content.

The boundary is written down with equal force: **task-queue URLs are never redacted**. They are keys for later requests — scrubbing signature parameters turns the next crawl into a 403. That exclusion, with its reasoning, lives in code comments and the engineering discipline so it is not "helpfully" broken later.

### 4.6 Media layer — direct URL extraction and lossless download

Four platform resolvers (Douyin, Xiaohongshu, Kuaishou, NetEase Cloud Music) dispatch through a table. The extraction doctrine is **read what the page already contains** (`window.__INITIAL_STATE__`, JSON-LD) before constructing any request parameter. All outputs converge on one schema, `MediaItem` (`source / media_id / kind / streams / images / method`), so callers read a single contract. Signature algorithms (e.g. the SM3 layer of Douyin's a_bogus) are implemented as public protocol facts and regression-tested against byte-exact vectors.

The extension contract carries two hard constraints born of incidents: resolvers are synchronous, so they must be invoked through `asyncio.to_thread` (a synchronous call once froze the event loop for 30–80 s on Douyin), and extracted direct URLs must re-pass `is_private_url` — the SSRF gate of §4.4 extended onto the media path.

The downloader guarantees **lossless and atomic**: probe size and Range support, stream to `.part`, resume at the exact byte offset, verify byte counts, then atomic rename — nothing downstream ever sees a partial file. m3u8 fragments decrypt AES-128 (pycryptodome); ffmpeg only merges fragments and **never re-encodes**; when a server lacks Range support the stale `.part` is discarded rather than concatenated into corruption. Video is byte transport; audio converts to MP3 only when the audio channel is explicitly enabled.

### 4.7 Liveness layer — watchdog and blocking offload

A per-page watchdog defaults to 300 s (normal pages finish under 60 s; a 5× margin), configurable, and can be disabled with `--page-timeout 0` — a flag whose pass-through was repaired in v2.19.8 after the "specified but never effective" defect. Timed-out pages are killed and marked for retry without touching the rest of the batch. The watchdog's partner is a repository-wide discipline: **every blocking call goes through `asyncio.to_thread`** — SimHash fingerprints, full-text redaction, file IO, synchronous resolvers all included. Nothing blocking is allowed on the event loop.

---

## 5. Extension contracts

**Site rules, zero code.** A new site is one YAML file under `rules/sites/`, validated by `rule-validate` and dry-run by `rule-test` against a local sample. All 12 rules ship with real-page HTML samples checked by the release gate, which is what makes the rule layer's regression fully offline. The `exclude` key keeps rules from fighting the video and music channels for URLs.

**Platform resolvers.** A new platform is one module returning the homogeneous `{ok, ...}` dict through `media_schema`, plus one line in the dispatch table, plus — if the platform is domestic — an entry in `exit_manager._DOMESTIC_DOMAINS` (63 raw entries, 59 unique), the direct-connection whitelist that exists because foreign proxy IPs trigger risk control on domestic sites (Bilibili's GeeTest is the recorded case).

**GUI settings, six-point wiring.** A setting becomes live only after six hops: widget creation → wiring → startup config → the `EngineBridge` translation table → `crawl()`'s explicit key table → the `DEFAULT_GLOBAL` defaults. **Missing any hop yields "fillable but inert" with no error**, which is why the checklist is written down and why `--page-timeout` needed until v2.19.8 to pass all six.

---

## 6. Quality apparatus

**Offline test line.** 675 cases (674 pass, 1 skip), zero network access by design: rules are regression-tested against real-page HTML fixtures, signature algorithms against byte-exact vectors, concurrency correctness against in-memory databases. The gate's criterion is fixed: ≥400 passed and 0 failed.

**Ten-check release gate.** `python tools/release_check.py` runs ten checks; any failure blocks the release: version consistency across four files · clean git tree (untracked counts as dirty) · full test suite · static analysis with a locked baseline (ruff 82/≤84, mypy 103/≤104 — may only go down) · dependency audit (pip-audit against the pinned versions; this release: no known vulnerabilities) · secret scan · credential hygiene · rule and asset integrity (12 rules load, 10/10 samples present) · live multi-site regression · build surface (with a hard abort when the PO-Token components are missing from the package).

**Dependency governance.** Every runtime and dev dependency is pinned; upgrades require re-running the gate and re-basing the locked static counts. Licenses are inventoried per component; the two GPLv3 components (the GUI framework and the PO-Token plugin) are what make the project GPL-3.0 as a whole (§8). CI re-runs version consistency, static checks, the test suite and a coverage report on GitHub Actions.

---

## 7. Measured properties

| Property | Value | Provenance |
|---|---|---|
| Engine modules / lines | 67 / 22,635 | `kiana_vnext_plus/*.py` |
| Desktop UI | 1,645 lines · 5 pages | `launcher_v9.py` |
| Tests | 675 cases (674/1/0) | `pytest --collect-only` |
| Dependencies | 25 + 6, all pinned | the two requirements files |
| Site rules / samples | 12 / 10-of-10 present | `rules/sites/`, gate check #8 |
| Queue tables / schema version | 8 / `user_version` 5 | `frontier.py` |
| Fallback tiers | 5 | `engine_router.py` |
| Platform resolvers | 4, one schema | `universal_downloader.py` |
| Lease heartbeat / watchdog | 60 s / 300 s | `page_processor.py` / `config.py` |
| Flow control | 20 per domain; `Retry-After` cap 300 s | `crawler.py` / `rate_limiter.py` |
| SSRF surface | 10-hop cap · 90 s DNS TTL · 25 private forms | `url_utils.py` |
| Redaction | 19 parameter classes · depth 6 | `sanitizer.py` |
| Domestic direct-connect list | 63 raw / 59 unique | `exit_manager.py` |
| Release gate | 10 checks; static baseline 82/103 (cap 84/104) | `tools/release_check.py` |
| Deliverable | two exes + NSIS installer · ≈542 MB | built artifacts |
| Known vulnerabilities | none (pip-audit) | measured 2026-09 |

---

## 8. Limits and open items

Stated plainly: (1) some platform resolvers carry **offline-only evidence**; there is no end-to-end verification against the live services, and without a login session they degrade to readable errors rather than workarounds. (2) A few live-site resolvers currently **fail with unisolated causes**; until isolated they remain open items, not asserted as platform changes. (3) The installer is **≈542 MB** — the price of zero runtime downloads (§3.1). (4) Coverage is **reported, not enforced** (historical baseline ≈52%); large-model features are off by default. (5) **Single-maintainer** is a fact of the project; the gate and the locked baselines are engineering responses to that reality, not a denial of it.

---

## 9. Licensing and provenance

The project is **GPL-3.0** as a whole — not by preference but by consistency: it ships two GPLv3 components (PySide6-Fluent-Widgets for the GUI; the bgutil-ytdlp-pot-provider plugin), and GPL-3.0 is the license consistent with redistributing them. The full decision chain is recorded in the [code provenance statement](来源与合规声明.md). All source code is the project's own implementation; platform-interface and protocol algorithms follow public specification, and the implementation neither copies upstream code expression nor credits upstream by name. Bundled assets: Roboto (Apache-2.0); Mochiy Pop One and Yusei Magic (SIL OFL 1.1, license texts shipped under `assets/fonts/`); three decorative/test illustrations from the author's personal collection, not original work of the project — see the [image asset provenance statement](ASSET-PROVENANCE.md); rights holders may request immediate removal.

---

## 10. Concluding remarks

The engineering value of kiana-crawler does not lie in the length of its feature list but in the pairing of every claim with a source and every boundary with a written reason: five tiers against five real interception techniques; lease and CAS against two concurrency incidents that actually happened; a hop-by-hop gate against an attack reproduced on the developer's own machine; ten checks against the complete set of ways a single maintainer's release goes wrong. For a user who needs a Windows collection machine that installs once, behaves predictably and produces clean output, it offers an answer that can be verified claim by claim. For every other scenario, §1 and §8 give the honest "no".

---

## Appendix A. Verification index

| Claim | How to verify |
|---|---|
| 67 modules / 22,635 lines | `ls kiana_vnext_plus/*.py \| wc -l`; per-file `wc -l` summed |
| UI 1,645 lines / five pages | `wc -l launcher_v9.py`; count `addSubInterface` calls |
| 675 test cases | `python -m pytest tests --collect-only -q` |
| 25 + 6 pinned dependencies | count `==` lines in the two requirements files |
| 12 rules / samples | `ls rules/sites/*.yaml`; gate check #8 output |
| 8 tables / schema v5 | `grep -c "CREATE TABLE IF NOT EXISTS" kiana_vnext_plus/frontier.py` |
| 60 s heartbeat | `asyncio.sleep(60)` in `page_processor.py` |
| 300 s watchdog | `page_timeout` default in `config.py` |
| 10 hops / 90 s / 25 forms | `max_hops` / `_HOST_CACHE_TTL` / private-form table in `url_utils.py` |
| 19 parameter classes / depth 6 | `_URL_SECRET_RE` / `_REC_MAX_DEPTH` in `sanitizer.py` |
| 59 direct-connect domains | deduplicated count of `_DOMESTIC_DOMAINS` in `exit_manager.py` |
| 10 gate checks / baselines | `check_*` functions and `RUFF_MAX`/`MYPY_MAX` in `tools/release_check.py` |
| ≈542 MB installer | file size of any `KianaVnextPlus-Setup-*.exe` |
| pip-audit verdict | `python -m pip_audit -r kiana_vnext_plus/requirements.txt` |
