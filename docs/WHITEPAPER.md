# kiana-crawler — Technical Whitepaper (v2.19.9)

**Design and Implementation of a Single-Machine Web Collection Engine for Windows**

Repository: <https://github.com/pingguozhu071-sys/kiana-crawler>
Document type: technical whitepaper, written for engineers evaluating the design and implementation quality of this project
Companion documents: [README](../README.md) (four languages) · [Code provenance & license statement](来源与合规声明.md) · [Image asset provenance](ASSET-PROVENANCE.md)

---

## Abstract

Modern websites defend automated collection on three planes — TLS fingerprinting at the transport layer, JavaScript challenges at the execution layer, and behavioural rate-and-trajectory detection at the session layer — while media platforms additionally seal their downloadable streams behind signed, time-limited payloads embedded in page state. This whitepaper describes how kiana-crawler answers those obstacles under a deliberately narrow product envelope: one operator, one Windows machine, no server. Reachability is resolved by a five-tier fallback scheduler, concurrency correctness by a lease-and-compare-and-swap protocol, egress security by a hop-by-hop SSRF gate shared by every outbound path, data hygiene by three redaction layers with explicitly written exclusions, and long-run stability by a two-level token bucket and a per-page watchdog. Quality is enforced offline by approximately 1,490 test cases and a fourteen-check release gate. Every quantitative claim below resolves to a source in the repository; Appendix A indexes the load-bearing ones together with a command that reproduces each.

**Keywords**: web collection; anti-bot resistance; concurrency correctness; SSRF; redaction; resumable download; release engineering

---

## 1. Problem and positioning

### 1.1 Failure modes

Four failure modes dominate single-machine collection, and media platforms add a fifth. Each entry below is a failure the design has to survive, not an inconvenience that retrying will clear.

| # | Failure mode | Concrete shape |
|---|---|---|
| 1 | **Unreachable content** | TLS-fingerprint gates reject non-browser clients; JS challenges filter non-executing requests; behavioural detection throttles high-frequency access. No single strategy covers all three, and real sites combine them, so the client must order several techniques by cost and escalate on demand. |
| 2 | **Concurrent corruption** | Duplicate processing of one page, results overwritten by racing workers, interleaved database writes — all silent. Silence is the worst property of this class: nothing crashes, and the first symptom is two copies of one page, days later. |
| 3 | **Dirty output** | Duplicates, empty shells, third-party phone numbers and e-mail addresses, session tokens inside URLs. Redaction has boundaries of its own: applied in the wrong place it breaks the pipeline — scrubbing the signature out of a queued URL turns the next request into a 403. |
| 4 | **Liveness collapse** | One hung page stalls a batch; a naive rate limiter turns retries into a never-terminating storm; a transient network fault classified as permanent failure keeps a long run from ever completing. |
| 5 | **Sealed media payloads** | Playable URLs live inside embedded page JSON, signed and expiring, so a generic downloader handed the page URL routinely misses the best quality. Re-downloading at another quality is not acceptable for large files, so the download path needs resume and byte-level verification. |

### 1.2 Design goals

The design is organised around those failure modes rather than around site coverage.

| Dimension | Problem | Answer in this project |
|---|---|---|
| Fetch | TLS fingerprints, JS challenges, behavioural detection | five-tier fallback scheduler (§3.1) |
| Parse | heterogeneous site shapes | generic article extraction + declarative site rules + embedded-JSON parsing (§4.1) |
| Data quality | duplicates, empty shells, personal data | content-fingerprint dedup + quality gate + three redaction layers (§3.5) |
| Stability | concurrency, long runs, hung pages | lease + CAS + two-level token bucket + watchdog + resumable download (§3.2–§3.7) |

### 1.3 Non-goals and explicit refusals

The single-machine envelope is a product decision, not a capability gap: it buys zero deployment, zero server maintenance and data that never leaves the machine. The corresponding refusals are stated up front, each with its reason.

- **No multi-user or multi-tenant operation.** Configuration, credentials and data layout all assume one operator on one machine; multi-tenancy would require a different credential and data model, not a login page.
- **No distributed crawl cluster.** The frontier's lease semantics are designed for many coroutines on one machine; scaling out needs a different queue protocol, and a forced extension would reduce the consistency guarantees of §3.2 to paper.
- **No crossing of account permissions.** Login state comes only from cookies the operator supplies; when they are insufficient the tool reports a readable error rather than a workaround. This is a product position, not a technical limit: the project solves "how to collect public content reliably", not "how to get past a permission wall".
- **No large-model features by default.** LLM-assisted capabilities are off unless explicitly enabled, which avoids the inverted dependency in which the tool cannot run without a model installed.
- **No personal data written unredacted.** Phone numbers, e-mail addresses and IP addresses are redacted before they reach disk (§3.5). This is the default and is disabled only by an explicit `--no-sanitize` on the command line or the corresponding switch in the interface, which records that choice.

---

## 2. System overview

### 2.1 Scale and delivery form

| Component | Scale |
|---|---|
| Engine (`kiana_vnext_plus/`) | 71 Python modules · 27,361 lines |
| Desktop UI (`launcher_v9.py`) | 3,652 lines · five-page FluentWindow (PySide6 + qfluentwidgets) |
| Tests | approximately 1,490 cases, fully offline |
| Dependencies | 25 runtime + 6 development, all pinned with `==` |
| Site rules | 14 declarative YAML files — 12 production rules, each shipping a real-page sample, plus 2 templates |
| Storage | SQLite · 10 tables (8 queue + 2 export) plus one FTS5 index · idempotent migrations via `PRAGMA user_version` (schema version 6) |
| Deliverable | two executables (PyInstaller onedir) + NSIS multilingual installer · measured ≈542 MB |

The installer size is a deliberate trade: Chromium, ffmpeg, the PO-Token Node service and the deno runtime all ship inside the package, so a fresh install runs with zero runtime downloads. `docs/BUILD.md` records the ≈1.8 GB installed footprint. The cost is paid by every download of the installer and by a release pipeline that must verify the bundled components; the benefit is that the first crawl behaves like the thousandth. The two executables divide the work: `KianaLauncher` is the five-page shell with the engine in-process, and `KianaCrawler` is the command-line form for scripted or unattended runs. Both share one engine package, and the difference is confined to the control surface — command-line arguments and GUI settings translate into the same engine configuration.

### 2.2 Process model: the engine is not a subprocess

`EngineBridge` runs the asyncio event loop on a daemon thread inside the GUI process and calls the engine entry `crawl()` in-process; `print` and `logging` are bridged into Qt signals. The payoff is direct state access — pause, resume and stop are function calls rather than message round-trips, one-process debugging works, and no inter-process protocol has to be packaged. The price is a hard rule (**the GUI never touches engine objects directly**, only the bridge's stop/pause/resume controls and a read-only reference) plus a cross-thread discipline: returning to the main thread uses `QApplication.postEvent`, not direct signal emission. Both rules exist because their violation caused real incidents. The consequence of the in-process choice is asymmetric: in a subprocess design an engine crash costs a child process, here it would cost the window, so the bridge must catch every engine failure and translate it into a documented exit code.

Exit codes are a fixed contract: 0 completed, 1 stopped by the user, 2 engine crash, 3 engine load failure. The interface and any scripted wrapper consume the same semantics.

### 2.3 The life of one page (17 stages)

Establish (1–3): task directory (`cli_<sha8>/`, holding the queue database, export tree, the `stats.jsonl` contract feed, `checkpoint.json` and logs) → seed routing → enqueue. Execute (4–12): claim batch under lease → pick exit node and concurrency slot → heartbeat → cache probe → five-tier routing → parse-bomb guard → redact → deduplicate → quality gate. Emit (13–17): CAS claim → persist/export → media enqueue → link discovery → statistics.

Every stage has a defined failure branch: a failed claim is dead-lettered, routing escalates tier by tier, a lost CAS writes nothing, and the watchdog kills a timed-out page without touching the rest of the batch.

### 2.4 Runtime data layout and credential governance

The runtime data root is decided by `config.data_root()`: by default `%LOCALAPPDATA%\KianaVnextPlus`; with `KIANA_PORTABLE=1` it moves to a `KianaData/` directory beside the executable. The portable root must **not** be `sys._MEIPASS`: under the PyInstaller onedir layout that resolves to `_internal`, and the uninstaller removes `_internal` with `RMDir /r`, so a data root placed there would be destroyed by the next upgrade. The directory beside the executable is unaffected by uninstaller cleanup.

Credentials follow one rule — a local secret does not leave the machine. Operator-supplied cookies are encrypted into the store by the `cookie-add` subcommand (Fernet, from `cryptography`); the master key is written as a DPAPI-protected `master.key.bin`; the key pattern and the portable data root are both covered by `.gitignore` and by gate checks 6 and 7 (secret shapes and credential literals, with untracked build output counted as dirty). `cookie-list` reports the health of the stored accounts. No credential is built into the program, and every example uses placeholder text that the scanners accept. Because the HTTP cache stores already-redacted content (§3.5), the cache directory is not a second leak surface.

---

## 3. Mechanisms, as layers of defence

### 3.1 Reachability layer — five-tier fallback

`engine_router.py` orders the tiers by cost: protocol channel (curl_cffi TLS impersonation) → stealth channel (source-level anti-detection) → TLS switch → real browser (patchright primary, playwright fallback) → direct connection. A failure or a challenge promotes the request; while the cheap tier succeeds, the browser process is never launched — which is why rendering additionally carries its own budget cap, preventing browser storms under concurrency. The cost of a ladder is that its rungs must stay consistent with one another, which is why the user-agent-to-TLS binding lives in exactly one selection function rather than in each tier.

Two details are load-bearing. **Interception and escalation use different signals**: a gated request returns 400, while 403 is reserved as the "escalate to the browser tier" signal — conflating them sends blocked URLs into browser renders that cannot succeed. **The stealth channel refuses JS injection**: runtime-property spoofing leaves detectable traces, so the engine relies on protocol-level fixes and launch-argument hygiene (patchright's Runtime.Enable leak repair), with an explicit distinction between stealth removal and UX removal.

Upstream of the ladder sit three optional switches, each answering a compliance or efficiency requirement: `--robots` judges robots.txt per domain before a link is enqueued; `--sitemap` seeds from a site's sitemap, turning "one entry point, whole site structure" into a declaration; the cookie store (§2.4) supplies login state for pages that need it and degrades to a readable error when the credentials are insufficient. None of the three changes fallback semantics: they decide which URLs are worth entering the ladder, the ladder decides what it costs to retrieve them.

### 3.2 Consistency layer — lease and compare-and-swap

The frontier queue (`frontier.py`) is eight SQLite tables migrated idempotently towards `PRAGMA user_version` 6; the export schema adds two more tables and an FTS5 index (§2.1). Correctness rests on two primitives.

**Lease.** Claiming writes `leased_at` and `lease_expires`; a 60-second heartbeat renews `lease_expires` — never `leased_at`, whose mutation would break the compare-and-swap loop. The separate expiry column exists because a page whose worst-case processing time exceeded the lease timeout used to be reclaimed while it was still being worked on, producing two coroutines on one URL and duplicate media and exports.

**CAS.** Persistence requires winning a compare-and-swap with tri-state semantics: won (write), lost (write nothing), and *database failure* (`None`, which must raise and retry; treating `None` as "lost" would silently swallow a locked database). Failure paths carry the same rigor: `mark_failed` updates carry `AND status != 'done'`, because a watchdog once returned completed pages to retry and caused full re-crawls with duplicate exports — now a regression case.

`database is locked` and "the lease changed hands" are two different events and must not share a branch: the first is a SQLite write-lock conflict, answered by a `busy_timeout` and a retry; the second is a legitimate handover by another worker. The tri-state CAS is what makes it impossible to swallow both in the same `else`.

Dead-lettering is the last level of the protocol: pages that exhaust their retries, or cross the throttle counter limit, enter a dead-letter state instead of circulating forever, so a job **always terminates**. Termination does not mean every page succeeded; it means every page has an outcome. Dead-lettered pages carry a failure category, `retry-dead` re-enqueues them in bulk (for example after switching egress IPs), and `errors` aggregates the distribution — which turns "how did this run go" into an answerable question.

The standing rule against races: **a read-modify-write across `await` is a real race; occupy the slot synchronously before awaiting.** The robots gate and the render budget are both built this way.

### 3.3 Flow-control layer — two-level token bucket, terminable retries

Rate limiting is a global-plus-per-domain two-level token bucket, but its decisive property is the **separation of retry semantics**: being throttled does not consume retry budget. Throttling increments a dedicated counter that dead-letters after repeated occurrences; only genuine transport failures increment `retry_count`. Without this separation a throttled task lives in the retry queue forever and the main loop's exit condition never fires — the job would not run slowly, it would never finish.

A second split sits beside it, at the transport boundary: transport exceptions must raise, while "blocked by the gate" or "protocol not allowed" returns an empty result. Conflating them either classifies a network blip as permanent failure, losing a legitimate retry, or treats a deterministic block as a transient fault and retries it all the way into the dead-letter state. Callers therefore decide retries with `except` and give up on the return value.

Concurrency is arbitrated across three levels — global, per domain and per exit node — and the minimum wins. The per-domain default is 20 and the per-exit default is 10; the global limit adapts to the host as `max(8, min(32, 2 × logical cores))` unless it is configured explicitly, which keeps a machine from running the placeholder value the raw configuration carries. Stop and resume controllers must return their claims and recompute: a leftover low value otherwise pins concurrency permanently, a failure this project has already fixed once. `Retry-After` is honoured but capped at 300 s, so one server cannot park a batch for hours.

### 3.4 Egress security layer — hop-by-hop SSRF

The most expensive lesson in the repository: **entry-point URL validation is insufficient**. An external image URL can answer 302 towards an internal address, and the HTTP library follows it — reproduced locally with two loopback services, with the internal response saved to disk as an ordinary image. Every outbound fetch therefore passes through `url_utils.py`, the most-imported module in the engine.

Collecting the check into one shared primitive, rather than letting each channel build its own, follows from how security checks fail: the engine has several outbound paths (main crawl, media download, m3u8 fragments, link discovery), and any self-built validator will eventually be forgotten by a later change. A shared primitive makes new outbound paths safe by default, and bypassing it takes a deliberate, conspicuous line of code.

- entry validation, automatic redirects disabled, **manual hop-by-hop re-validation** (curl_cffi follows up to 30 redirects by default and its response object has no `.url` attribute, so re-validation can only be manual);
- hostname normalization before judgement — IPv6 zone IDs and trailing-dot FQDNs both once slipped past the blocklist;
- **25 adversarial private-address samples** in the regression suite, covering decimal, hexadecimal and octal IPv4 notations, IPv4-mapped IPv6, CGNAT, the 6to4 relay range and cloud metadata hostnames — forms the standard library's own predicates miss;
- **DNS verdicts expiring after 90 s**, which defeats resolve-public-then-repoint-internal rebinding;
- a **10-hop redirect cap**.

The cost of this layer is paid on the hot path: redirect handling is manual, every hop costs a check, and the DNS verdict cache needs its own expiry policy so that it neither misses a rebinding window nor queries per page. One exception is documented rather than hidden: `llm_client` intentionally carries no gate, because its endpoints are operator-supplied local inference services (a loopback Ollama endpoint, for example) that a gate would break. Security mechanisms may have justified exceptions, but the justification lives in the comments.

### 3.5 Data hygiene layer — three redaction layers with written exclusions

Redaction covers three leak surfaces. **Logs**: the filter attaches to handlers — Python logging invokes ancestors' handlers but not ancestors' logger filters, so attaching to the root logger is a no-op; new handlers receive redaction automatically through a wrapped `logging.Handler.__init__`. **Side channels**: the print bridge, the GUI log path and exception echoes each pass through redaction. **Artifacts**: exported copies are scrubbed record-wide by `sanitize_record`, recursing six levels deep. Nineteen sensitive URL-parameter classes (`token`, `key`, `sign`, `session`, `csrf` and kin) plus phone, e-mail and IP patterns are covered; the HTTP cache stores sanitized content.

Parameter recognition uses an **explicit list rather than a fuzzy match**. A generic rule such as "drop any parameter whose name contains key" has an uncontrollable false-positive rate and will delete business parameters along with secrets; the explicit list costs a registration step for every new parameter name, but each entry is reviewable. This is the same trade as the enumerated private-address forms of §3.4: prefer a small miss over a wide break.

The boundary is written down with equal force: **task-queue URLs are never redacted**. They are keys for later requests — scrubbing signature parameters turns the next crawl into a 403 — so the exclusion, with its reasoning, lives in code comments and in the regression suite, where a later contributor cannot "helpfully" close it.

The export path has a byte-level boundary case of the same kind: the CSV UTF-8 BOM may appear **only once, on the first write of a new file**; append mode must not use `utf-8-sig`, or every append inserts another BOM mid-file and corrupts the columns from the second batch onwards. This was a real defect and is now a regression case.

### 3.6 Media layer — direct URL extraction and lossless download

Four platform resolvers (Douyin, Xiaohongshu, Kuaishou, NetEase Cloud Music) dispatch through a domain table. The extraction doctrine is **read what the page already contains** (`window.__INITIAL_STATE__`, JSON-LD) before constructing any request parameter. All outputs converge on one schema, `MediaItem` (`source / media_id / kind / streams / images / method`), so callers read a single contract instead of four response shapes. Signature algorithms (for example the SM3 layer of Douyin's a_bogus) are implemented as public protocol facts and regression-tested against byte-exact vectors, so implementation drift is caught before it ships rather than after a site changes its response.

The extension contract carries two hard constraints born of incidents: resolvers are synchronous, so they must be invoked through `asyncio.to_thread` (a synchronous call once froze the event loop for 30–80 s on Douyin), and extracted direct URLs must re-pass `is_private_url` — the SSRF gate of §3.4 extended onto the media path, because a URL taken from page state is attacker-influenced input.

The downloader guarantees **lossless and atomic**: probe size and Range support, stream to `.part`, resume at the exact byte offset, verify byte counts, then atomic rename — nothing downstream ever sees a partial file. m3u8 fragments decrypt AES-128 (pycryptodome); ffmpeg only merges fragments and **never re-encodes**; when a server lacks Range support the stale `.part` is discarded rather than concatenated into corruption. Video is byte transport; audio converts to MP3 only when the audio channel is explicitly enabled. Optional components follow one rule: an absent optional capability must not fail the task — the Bilibili danmaku ASS converter is skipped when it is not installed, while the danmaku data itself is still collected.

### 3.7 Liveness layer — watchdog, blocking offload, and two Windows failure modes

A per-page watchdog defaults to 300 s (normal pages finish under 60 s; a 5× margin), is configurable, and can be disabled with `--page-timeout 0` — a flag whose pass-through was repaired in v2.19.8 after the "specified but never effective" defect. Timed-out pages are killed and marked for retry without touching the rest of the batch. The margin is the trade: a hung page costs up to five minutes before it is reclaimed, which is the price of not killing legitimately slow pages.

The watchdog's partner is a repository-wide discipline: **every blocking call goes through `asyncio.to_thread`** — SimHash fingerprints, full-text redaction, file IO and synchronous resolvers all included. Nothing blocking is allowed on the event loop.

Two Windows-specific failure modes are recorded with their fixes because both fail silently. **Subprocess encoding**: when yt-dlp drives ffmpeg, the child process output is decoded with the system codepage, so non-ASCII file names corrupt the merge and can end in an empty output folder; the fix is ASCII hash file names plus forcing a UTF-8 locale for the child. **Non-ASCII image paths**: `cv2.imread` returns `None` for a path containing non-ASCII characters on Windows instead of raising, so the wallpaper path reads bytes with `np.fromfile` and decodes with `cv2.imdecode`. Both share the same property — no exception, no log line — and the next maintainer concludes the feature is broken while the code is correct.

### 3.8 Observability and recoverability

A long run has to answer two questions: what is it doing now, and can it be resumed. Statistics are appended to `stats.jsonl`, consumed by both the GUI statistics cards and the CLI `status` subcommand through the same contract; failure and error records are aggregated by the `errors` subcommand, which turns "how far did this run get, and which class of site failed" into an answerable question. For recovery, `checkpoint.json` in the task directory records the resume point, failed pages can be re-enqueued from the dead-letter state with `retry-dead` instead of restarting the job, the queue itself is persistent SQLite, and the export tree is written per domain (JSONL / CSV) with media archived by kind — a task directory is self-contained and can be copied elsewhere for analysis.

The trade of this design is that it is file-first: no observation service or additional database dependency is introduced, and every piece of state is a file that opens in a text editor. For a single-machine tool, a `stats.jsonl` that opens instantly is more reliable than a panel that requires starting a service first, and diagnosis stays possible on an offline machine. The cost is the absence of live dashboards and of any aggregation across machines.

---

## 4. Extension contracts

### 4.1 Site rules, zero code

A new site is one YAML file under `rules/sites/`, generated as a skeleton by `rule-new`, validated by `rule-validate`, and dry-run by `rule-test` against a local sample — a write-validate-regress loop that stays offline. Of the 14 rule files in the tree, 12 are production rules that each ship an HTML sample captured from the real page, and the release gate verifies the sample inventory (12 of 12 registered domains present); the remaining two are templates. The `exclude` key keeps rules from competing with the video and music channels for URLs.

The consequence is that rule-level regression depends on no live site: a site redesign does not turn the suite red overnight, and is surfaced by the live regression line (gate check 9) instead. The trade is that every rule carries a captured sample as a repository artifact, and that samples age — which is why the structural fingerprint check (§5.2) exists, to detect a sample that was replaced, truncated or re-encoded.

### 4.2 Platform resolvers

A new platform is one module returning the homogeneous `{ok, ...}` dict through `media_schema`, plus one line in the dispatch table, plus — if the platform is domestic — an entry in `exit_manager._DOMESTIC_DOMAINS` (63 raw entries, 59 unique), the direct-connection list that exists because foreign proxy IPs trigger risk control on domestic sites (Bilibili's GeeTest challenge is the recorded case).

"Homogeneous `{ok, ...}` dict" is the load-bearing part: a resolver failure is data (`ok: false` with a reason), not an exception, so the download chain can treat "this platform did not help" as a reason to keep the original URL on the generic path instead of aborting the batch. Direct-URL extraction goes through the schema as well (preferring `media.streams[0].url`, falling back to platform-specific keys), so callers need no per-platform knowledge. The result is that adding a platform stays "one file plus one line", and the two hard constraints (`to_thread`, private-address re-check) are written into the contract rather than left to the extender's memory.

### 4.3 Configuration consistency — six-point wiring

A GUI setting becomes live only after six hops: widget creation → wiring → startup configuration → the `EngineBridge` translation table → `crawl()`'s explicit key table → the `DEFAULT_GLOBAL` defaults. **Missing any hop yields "fillable but inert" with no error**, which is why the checklist is written down and checked; `--page-timeout` needed until v2.19.8 to pass all six.

One principle runs through the configuration system: **`DEFAULT_GLOBAL` is the single source of engine defaults.** The interface layer (`launcher_config.json`) and the engine layer each hold a configuration, and the bridge's translation table only moves keys from the interface's spelling to the engine's; defaults must not be written twice, or changing one copy leaves the other behind and the same parameter behaves differently depending on the entry point. `page_timeout_override()` is a pure function for the same reason: it concentrates the "only an explicit value overrides" decision in one unit-testable place — an absent command-line argument yields nothing and the engine falls back to 300 s, while an explicit 0 passes through unconditionally and the watchdog is genuinely disabled. "Unspecified" and "explicitly zero" become two distinguishable states, which is the class of defect most likely to be misjudged during an incident review.

### 4.4 Desktop UI engineering — five pages and the wallpaper pipeline

The desktop interface has five pages (home, logs, data, tasks, settings) built on the qfluentwidgets `FluentWindow`. Two disciplines come from incidents. **Page construction runs once, in `__init__`**: an indentation accident once left the bridge construction and page building inside a resize handler, so every show, resize and window activation rebuilt the bridge, the five pages, the navigation entries and the application theme — the theme pass alone touches about 1,910 widgets at roughly 1.2 s per application. After the fix the rebuild cost is zero. **Statistics cards must be registered explicitly**: an earlier implementation located them by scanning the widget tree for object names with `findChildren`, so any change to the tree produced cards that were wired but never updated, silently; the current registry makes a missing registration visible in review.

The wallpaper pipeline is the heaviest image-processing block in the interface: single image or folder rotation, cover cropping with a nine-grid focal point, a 0–30 Gaussian blur, an automatic overlay driven by luminance analysis (more overlay on bright images), two-layer cross-fade, and accent-colour extraction from the wallpaper (HSV filtering, then the histogram mode, lockable to a manually chosen colour that is mutually exclusive with extraction mode). Parameter changes are debounced by 200 ms and cached by `(path, size, blur, overlay, focus)`, so resizing does not recompute resampling; heavy work runs in `to_thread`, and Qt graphics objects are constructed only on the main thread. The electronic signature watermark in the lower corner is **empty by default**: the program ships no default text, and whether it appears, and what it says, is entered by the operator.

---

## 5. Quality apparatus

### 5.1 Offline test line

Approximately 1,490 cases (1,483 test functions, three of them parametrized) run with zero network access by design: site rules are regression-tested against real-page HTML fixtures, signature algorithms against byte-exact vectors, and concurrency correctness against in-memory databases. Offline is a design choice rather than a compromise — a suite that needs a live site cannot serve as a gate. The gate's criterion is fixed: at least 400 passed and 0 failed.

The suite is organised by delivery batch (`test_v218_*`, `test_v219_*`, `test_v2198_*`), each batch corresponding to the mechanisms introduced and the defects fixed in one release. That organisation buys traceability — a failing case names the batch of mechanisms it guards — and makes it easier to tell invariants from implementation details during a refactor. Cases that need an unavailable environment (a browser kernel, a Node runtime, a live solver) are skipped conditionally, and the skip mechanism itself is covered by the gate's counting rule.

### 5.2 The fourteen-check release gate

`python tools/release_check.py` runs fourteen checks; any failure blocks the release. The first ten are the original release checklist; the last four were added after consecutive review rounds showed that whole classes of defect had no check at all.

| # | Check | What it establishes |
|---|---|---|
| 1 | Version consistency | `VERSION.json`, `pyproject.toml`, `__init__.py` and the NSIS script agree |
| 2 | Git hygiene | clean worktree; untracked files count as dirty |
| 3 | Full test suite | at least 400 passed and 0 failed |
| 4 | Static analysis | ruff 80 and mypy 99 against locked ceilings of 80 and 99, plus the F-class (real-defect) subset over `tools/` and `tests/` at 42; a locked baseline may only go down |
| 5 | Dependency audit | pip-audit against the pinned versions (this release: no known vulnerabilities) |
| 6 | Secret scan | no private-key or live API-key shapes inside the shipped package |
| 7 | Credential hygiene | no cookies or credential literals in the repository; `.gitignore` covers cookie artifacts |
| 8 | Rule and asset integrity | all 14 rule files load; 12 of 12 registered real-site samples present |
| 9 | Live multi-site regression | `verify_all` across the probe lines, judged solely by its overall verdict line |
| 10 | Build surface | entry points compile and the spec exists; missing PO-Token components abort packaging |
| 11 | Structural fingerprint | sample fingerprints match the committed baseline; the offline half always runs, and the online half reports SKIP (exit code 2) rather than a false PASS when it cannot measure |
| 12 | Silent-failure scan | hot-path `except: pass` blocks without a reason comment, against a locked baseline that may only go down |
| 13 | Redaction chain | live probe strings (URL signature parameters, Cookie headers, e-mail addresses) must not appear in handler output |
| 14 | Duplicate-capability scan | same-named functions in different modules with mismatched signatures block the release; names appearing in three or more modules are registered only |

The design philosophy is one sentence: **turn "things that should be checked before a release" into "things that must pass before a release"**. Each check corresponds to a failure mode specific to a single-maintainer project — a version bumped in three of four places, an untracked debugging artifact left in the tree, static findings creeping back up, a key pasted into an example, a rule added without its sample. The gate has no "skip with a warning" state: FAIL exits 1, and SKIP must be declared with a reason (for example `--skip-network`). Locking the static baseline so that it may only decrease is the least intuitive and most effective item: it does not demand perfect code, only that the code not get worse than yesterday, which is the realistic goal for a repository maintained by one person. The cost is that every improvement must be re-baselined deliberately, and that a check which cannot measure reports SKIP instead of green.

### 5.3 Dependency governance and supply chain

All 25 runtime and 6 development dependencies are pinned with `==`; upgrades require re-running the gate and re-basing the locked static counts. Licences are inventoried per component: the two GPLv3 components (the GUI framework and the PO-Token plugin) are what make the project GPL-3.0 as a whole (§8), and the fonts and illustrations that ship with the package have separate provenance statements. GitHub Actions re-runs version consistency, the static checks, the full test suite and a coverage report on a Windows runner; the coverage report carries no threshold (§7). Two limits are worth stating: the pinned set is a configuration verified on one machine rather than a matrix across Python versions, and CI covers only the offline checks — packaging and the live regression line remain manual.

### 5.4 Build and release engineering

The release chain is one script, `一键构建.bat`, organised in seven numbered stages with three sub-stages: interpreter check (1), dependency check (2), Chromium download (3), syntax check (4) with the static checks (4c), behaviour tests (4b) and PO-Token component checks (4d), the dual PyInstaller build (5), NSIS packaging (6) and archiving (7). Two spec files correspond to the two executables — `KianaCrawler.spec` for the command-line form and `KianaLauncher.spec` for the five-page shell — and both share one engine package; the spec decides only the entry point and the resource collection surface.

The hard constraints on the build side also come from incidents:

- **Missing PO-Token components abort the build.** The spec checks for the vendored directory (the Node service and the deno runtime) and raises `SystemExit` when it is absent; the escape hatch `KIANA_SKIP_POT=1` is reserved for test builds. The reason is a release that packaged successfully without the components and failed only after installation.
- **An integrity check runs on the packaged output**: `_internal` must contain the deno executable, the PO-Token server entry and the browser binary, and a missing one fails the build.
- **qfluentwidgets resources must be collected** (`collect_data_files`); the symptom of forgetting is a white window after packaging, because the package carries SVG, QSS and font assets that code-only collection does not see.
- **The NSIS installer's language files are generated artifacts** (produced by `tools/gen_nsis_langs.py`, not hand-edited), and the uninstaller is three-branch conditional logic: installed from the registry and restorable, it restores the previous installation; installed from the registry and not restorable, it cleans up; with no registry entry, it removes only itself. The shortcut-restoration branch once pointed restored shortcuts at a temporary directory, because NSIS `CreateShortCut` takes the current `$OUTDIR` as its start-in path; v2.19.8 fixed it by setting `SetOutPath` explicitly to the restored installation directory.
- **Under Git Bash, makensis must be invoked through a Python subprocess**: MSYS rewrites `/S`-style switches into `S:/` paths.
- **The install/uninstall matrix tool** (`tools/installer_matrix.py`) covers eight language IDs and describes the install, verify and uninstall passes per language; `--dry` short-circuits before any system change, which matters because a dry run that still installed and uninstalled would touch the machine's real installation.

### 5.5 Regression cases — from incident to assertion

The suite is not assembled to raise a coverage number; **every real incident is pinned as a regression case**. Representative examples:

| Incident | Regression assertion |
|---|---|
| A risk-control downgrade pinned global concurrency permanently (a recovered controller did not return its claim) | after recovery, min-wins claims must be returned |
| The watchdog returned completed pages to retry (full re-crawl and duplicate exports) | the `mark_failed` update carries `AND status != 'done'` |
| `database is locked` was swallowed as "the lease changed hands" | tri-state CAS: `None` must raise |
| Throttled tasks stayed in the retry queue and the main loop never exited | throttling does not increment `retry_count`; it has its own dead-letter counter |
| A CSV append wrote a second BOM and corrupted the second batch | the BOM is written once, on the first write of a new file |
| A bare `session.get` followed a 302 and stored an internal response as an image | all 25 adversarial private-address samples are blocked |
| A resolver's direct URL skipped the private-address re-check | every media direct URL passes `is_private_url` |
| `--page-timeout 0` never reached the engine | explicit values, including 0, pass through; 0 disables the watchdog |

This table is the reason the mechanisms of §3 exist: they were not designed in the abstract, they were forced by incidents and then pinned by cases. A new contributor reading the tests can use the table as an index into the source, with each assertion standing for a story about what happens without it.

---

## 6. Measured properties

The table below consolidates the quantitative statements of this document. Conventions: line counts are raw `wc -l` lines, including blank and comment lines; module counts are the `.py` files under `kiana_vnext_plus/`, excluding the entry scripts and the UI; test counts are test functions; installer size is the file size of a built NSIS artifact. Every value can be re-checked independently with the command given in Appendix A — the table invites verification, which is what separates it from a marketing page.

| Property | Value | Provenance |
|---|---|---|
| Engine modules / lines | 71 / 27,361 | `kiana_vnext_plus/*.py` |
| Desktop UI | 3,652 lines · 5 pages | `launcher_v9.py` |
| Tests | ≈1,490 cases (1,483 test functions) | `tests/` |
| Dependencies | 25 + 6, all pinned | the two requirements files |
| Site rules / samples | 14 files / 12 real-site samples | `rules/sites/`, `tests/assets/`, gate check 8 |
| Tables / schema version | 10 (8 queue + 2 export) + 1 FTS5 | `frontier.py`, `enhancements.py` |
| Fallback tiers | 5 | `engine_router.py` |
| Platform resolvers | 4, one schema | `universal_downloader.py` |
| Lease heartbeat / watchdog | 60 s / 300 s | `page_processor.py` / `config.py` |
| Flow control | 20 per domain; host-adaptive global; `Retry-After` cap 300 s | `identity.py` / `crawler.py` / `protocol_engine.py` |
| SSRF surface | 10-hop cap · 90 s DNS TTL · 25 adversarial samples | `url_utils.py`, `tests/test_v219_ssrf.py` |
| Redaction | 19 parameter classes · depth 6 | `sanitizer.py` |
| Domestic direct-connect list | 63 raw / 59 unique | `exit_manager.py` |
| Release gate | 14 checks; static baseline 80/99, F-class 42 | `tools/release_check.py` |
| Deliverable | two executables + NSIS installer · 542 MB | built artifacts |
| Exit-code contract | 0 completed / 1 stopped / 2 crash / 3 load failure | `launcher_v9.py` |
| Installer language IDs | 8 | `tools/installer_matrix.py` |
| Known vulnerabilities | none (pip-audit) | measured 2026-09 |

---

## 7. Limits and open items

Stated plainly: (1) some platform resolvers carry **offline-only evidence**; there is no end-to-end verification against the live services, and without a login session they degrade to readable errors rather than workarounds. The live regression line (gate check 9) is the mitigation, and it records unavailability as SKIP rather than as success. (2) A few live-site resolvers currently **fail with unisolated causes**; until isolated they remain open items, not asserted as platform changes, and the `errors` subcommand is what supplies evidence for isolating them. (3) The installer is **542 MB** — the price of zero runtime downloads (§2.1); the mitigation is a single delivery plus a uninstaller that cleans up in three branches (§5.4), not a smaller package. (4) Coverage is **reported, not enforced**: CI publishes a coverage report and no threshold is configured. The reason is that this suite's primary source is incident regression rather than line coverage, so line coverage is not a valid quality signal at this stage. (5) Large-model features are off by default. (6) **Single maintainer** is a fact of the project: the fourteen checks and the locked baselines are engineering responses to that reality, not a denial of it, and every mechanism and pitfall is written down in-repository (this document, `docs/ARCHITECTURE.md`, `docs/BUILD.md`, `tools/`) so that handover does not depend on one person's memory.

---

## 8. Licensing and provenance

The project is **GPL-3.0** as a whole — not by preference but by consistency: it ships two GPLv3 components (PySide6-Fluent-Widgets for the GUI, and the bgutil-ytdlp-pot-provider plugin), and GPL-3.0 is the licence consistent with redistributing them. In practice that means the project may be used, modified and redistributed freely, with source provided under GPL-3.0 on redistribution; a project that needs different terms must first replace those two components. The full decision chain is recorded in the [code provenance statement](来源与合规声明.md).

**Code provenance.** The codebase continues an earlier internal baseline and advances it substantially; it is not a from-zero build. All source code in this repository is the project's own implementation; platform-interface and protocol algorithms (request-parameter construction, the choice of hash and encryption chains, streaming fragment formats) are public protocol facts — any implementation must follow the same rules, and they are not copyrightable expression. The implementation does not copy upstream code expression and does not name upstream projects in comments; where a design idea was adopted, the comment records the design goal and the general approach rather than a source.

**Engineering self-discipline.** Third-party code is licence-checked before it is introduced, copyleft components are not taken on as *new* dependencies (the two existing GPLv3 components are historical decisions, kept consistent by the project-wide licence), and code of unknown origin is not used. The internal security audits referenced by this document are self-assessments; they do **not** constitute a security certification, and nothing in this document is legal advice.

**Bundled assets.** Roboto (Apache-2.0); Mochiy Pop One and Yusei Magic (SIL OFL 1.1, with the licence texts shipped under `assets/fonts/`); three decorative and test illustrations from the author's personal collection, not original work of the project — see the [image asset provenance statement](ASSET-PROVENANCE.md); rights holders may request immediate removal.

---

## 9. Conclusion

The engineering value of kiana-crawler does not lie in the length of its feature list but in the pairing of every claim with a source and every boundary with a written reason: five tiers against five real interception techniques; lease and CAS against two concurrency incidents that actually happened; a hop-by-hop gate against an attack reproduced on the developer's own machine; fourteen checks against the ways a single maintainer's release goes wrong. Compressed to one paragraph: the project turns the three most common ways a personal project dies — silent concurrency faults, silent security holes, and silently inert configuration — into explicit contracts (tri-state CAS, the hop-by-hop gate, six-point wiring), and pins those contracts with a gate in which any single failure blocks the release. Its credibility comes not from promises but from the fact that any sentence in this document can be checked within five minutes of opening the repository. For a user who needs a Windows collection machine that installs once, behaves predictably and produces clean output, that is an answer which can be verified claim by claim; for every other scenario, §1.3 and §7 give the honest "no". For readers interested in engineering practice, `tools/release_check.py` and `tests/` are a better starting point than this document: the mechanisms are in the code and the incidents are in the cases.

---

## Appendix A. Verification index

Every command below was executed against the working tree this document describes, and each prints the value in the left column; the rows that name a file instead of a command point at the artifact that establishes the claim.

| Claim | How to verify |
|---|---|
| 71 modules / 27,361 lines | `ls kiana_vnext_plus/*.py \| wc -l` → 71 · `wc -l kiana_vnext_plus/*.py \| tail -1` → `27361 total` |
| UI 3,652 lines / five pages | `wc -l launcher_v9.py` → 3652 · `grep -c 'addSubInterface(self\.' launcher_v9.py` → 5 |
| ≈1,490 test cases | `grep -rhE '^[[:space:]]*(async )?def test_' tests \| wc -l` → 1,483 test functions; `grep -rn '@pytest.mark.parametrize' tests` → 3 decorators, over 2, 5 and 5 inputs, so the collected total is 1,492 |
| 25 + 6 pinned dependencies | `grep -cE '^[A-Za-z0-9_.-]+==' kiana_vnext_plus/requirements.txt requirements-dev.txt` → 25 and 6 |
| 14 rule files / 12 real-site samples | `ls rules/sites/*.yaml \| wc -l` → 14 · `ls tests/assets/*.html \| wc -l` → 12 |
| 10 tables + 1 FTS5 index / schema version 6 | `grep -oE 'CREATE TABLE IF NOT EXISTS [a-z_]+' kiana_vnext_plus/frontier.py kiana_vnext_plus/enhancements.py \| wc -l` → 10 · `grep -hoE 'CREATE VIRTUAL TABLE [a-z_]+' kiana_vnext_plus/enhancements.py \| sort -u` → `CREATE VIRTUAL TABLE scrape_fts` · `grep -n 'SCHEMA_VERSION = 6' kiana_vnext_plus/frontier.py` → `SCHEMA_VERSION = 6` |
| 60 s lease heartbeat | `grep -n 'asyncio.sleep(60)' kiana_vnext_plus/page_processor.py` → `await asyncio.sleep(60)` |
| 300 s watchdog | `grep -n '"page_timeout": 300' kiana_vnext_plus/config.py` → `"page_timeout": 300,` |
| 20 per domain · host-adaptive global · 300 s `Retry-After` cap | `grep -n '"rate_limit_per_domain": 20' kiana_vnext_plus/identity.py` → `"rate_limit_per_domain": 20` · `grep -n 'min(32, _cores \* 2)' kiana_vnext_plus/crawler.py` → `max(8, min(32, _cores * 2))` · `grep -n 'RETRY_AFTER_CAP_SECONDS = 300' kiana_vnext_plus/protocol_engine.py` → `RETRY_AFTER_CAP_SECONDS = 300` |
| 10-hop redirect cap / 90 s DNS TTL | `grep -n 'max_hops: int = 10' kiana_vnext_plus/url_utils.py` → `max_hops: int = 10` · `grep -n '_HOST_CACHE_TTL = 90' kiana_vnext_plus/url_utils.py` → `_HOST_CACHE_TTL = 90.0` |
| 25 adversarial private-address samples | `sed -n '57,60p;73,79p' tests/test_v219_ssrf.py \| grep -oE '"[a-z]+://[^"]+"' \| wc -l` → 25 |
| 19 parameter classes / recursion depth 6 | `sed -n '14,15p' kiana_vnext_plus/sanitizer.py \| tr '\|' '\n' \| grep -oE '[a-z_-]{2,}' \| wc -l` → 19 · `grep -n '_REC_MAX_DEPTH = 6' kiana_vnext_plus/sanitizer.py` → `_REC_MAX_DEPTH = 6` |
| 63 raw / 59 unique direct-connect domains | `sed -n '42,56p' kiana_vnext_plus/exit_manager.py \| grep -o '"[a-z0-9.-]*"' \| wc -l` → 63; the same pipeline with `sort -u` → 59 |
| 5 fallback tiers | `grep -n 'FALLBACK_[A-Z]* = ' kiana_vnext_plus/engine_router.py` → five constants, `FALLBACK_PROTOCOL` through `FALLBACK_DIRECT` |
| 4 platform resolvers | `grep -oE '"[a-z0-9_]+_resolver"' kiana_vnext_plus/universal_downloader.py \| sort -u \| wc -l` → 4 |
| 14 gate checks / baselines 80, 99, 42 | `grep -c 'checks.append((' tools/release_check.py` → 14 · `grep -n 'RUFF_MAX, MYPY_MAX = 80, 99' tools/release_check.py` → `RUFF_MAX, MYPY_MAX = 80, 99` · `grep -n 'TOOLS_F_MAX = 42' tools/release_check.py` → `TOOLS_F_MAX = 42` |
| Exit-code contract 0/1/2/3 | `grep -n 'rc == ' launcher_v9.py` → `rc == 2`, `rc == 1`, `rc == 3`, `rc == 0` in the completion handler |
| 17 pipeline stages | `sed -n '143,145p' README.md \| tr '→' '\n' \| grep -c '[a-z]'` → 17 |
| 542 MB installer | `du -m KianaVnextPlus-Setup-*.exe` → 542 per installer (raw sizes 541.2–541.5 MiB) |
| 8 installer language IDs | `grep -n 'LANG_IDS = ' tools/installer_matrix.py` → `LANG_IDS = [2057, 1033, 2052, 1028, 3076, 1041, 1042, 1036]` |
| Seven-stage build script | `grep -oE '\[[0-9][a-d]?/7\]' 一键构建.bat \| sort -u` → 1/7 … 7/7 plus 4b/4c/4d |
| Coverage reported, not enforced | `.github/workflows/ci.yml` runs a coverage report step with no threshold; `pyproject.toml` configures none |
| Six-hop settings wiring | audited by `tests/test_config_wiring_audit.py` |
| pip-audit verdict (dated observation) | `python -m pip_audit -r kiana_vnext_plus/requirements.txt` |
