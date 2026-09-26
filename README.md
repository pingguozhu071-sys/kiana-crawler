# kiana-crawler

[简体中文](README.zh-CN.md) ｜ **English** ｜ [日本語](README.ja-JP.md) ｜ [한국어](README.ko-KR.md)

> A single-machine web collection engine for Windows. One run takes a set of URLs through fetch → parse → deduplicate → export and writes pages, video and audio to local disk.
> Design rationale in depth: **[Technical Whitepaper](docs/WHITEPAPER.md)** (English) · [技术白皮书](docs/WHITEPAPER.zh-CN.md) (Chinese)

**Desktop GUI + CLI** · Python 3.11+ · Distributed as a single installer

---

## Contents

- [What it is](#what-it-is)
- [At a glance](#at-a-glance)
- [Technical highlights](#technical-highlights)
- [Architecture](#architecture)
- [Quick start](#quick-start)
- [Boundaries & honest notes](#boundaries--honest-notes)
- [License & provenance](#license--provenance)

---

## What it is

A **single-machine collection engine** for Windows: a self-contained asyncio crawler core, with a PySide6 desktop interface as the control surface.

The design is organized around four problems rather than around site coverage:

| | Problem | Approach |
|---|---|---|
| **Fetching** | Sites gate content with TLS fingerprinting, JS challenges and behavioural detection | Five-tier fallback: protocol → stealth → TLS switch → real browser → direct |
| **Parsing** | Markup differs from site to site | Generic article extraction + declarative site rules + embedded-JSON parsing |
| **Data quality** | Duplicates, empty shells and private data reaching the output | Content-fingerprint dedup + quality gate + three-layer redaction |
| **Stability** | Dozens of concurrent requests, multi-hour runs, pages that hang | Lease + CAS consistency + per-page watchdog + token-bucket rate limiting + resumable downloads |

---

## At a glance

| Item | Value |
|---|---|
| Engine | **67 modules · 22,635 lines** |
| Desktop UI | **1,645 lines** · five-page FluentWindow |
| Tests | **675 cases** (674 pass / 1 skip / 0 fail) · **fully offline** |
| Dependencies | **25** runtime + **6** dev, **all pinned with `==`** |
| Site rules | **12** declarative YAML files — a new site is one file, zero code |
| Proxy routing | **59** domestic domains forced to direct connection (proxy bypass) |
| Concurrency | Single-process asyncio · **20–50 coroutines** |
| Queue | SQLite, **8 tables**, versioned migrations (`PRAGMA user_version`) |
| Release gate | **10 checks**, any failure blocks the release |
| Deliverable | Two exes (PyInstaller onedir) + NSIS multilingual installer · ~542 MB |

---

## Technical highlights

Each highlight follows the same four-part structure: **problem → implementation → key figures → modules**.

### ① Five-tier fallback: content reachability

> **Problem**: Sites differ in how they gate content. Some serve it directly; some fingerprint the TLS handshake; some render only after real JS execution. No single strategy covers all three.

- **Implementation**: Five tiers ordered by cost — protocol (TLS impersonation) → stealth → TLS switch → real browser → direct. A failure or a challenge promotes the request to the next tier. If the cheap path succeeds, the browser is never launched.
- **Key figures**: 5 tiers · 20–50 concurrent coroutines · browser rendering has its own budget cap (no browser storms)
- **Modules**: `engine_router.py` (scheduling) · `protocol_engine.py` · `solver_engine.py`

### ② Platform resolvers: direct media URL extraction

> **Problem**: Media sites keep playable URLs inside embedded JSON, signed and time-limited. Handing the page URL to a generic downloader frequently misses the highest available quality.

- **Implementation**: **Read what the page already contains** (`window.__INITIAL_STATE__`, JSON-LD) before constructing any request parameters. Four platform resolvers, all converging onto one schema (`MediaItem`: source / media_id / kind / streams / images / method) so callers read a single field. URLs extracted from page state are re-checked through the SSRF gate before use.
- **Key figures**: 4 resolvers (Douyin / Xiaohongshu / Kuaishou / NetEase Cloud) · one unified schema · every extracted URL re-checked
- **Modules**: `media_schema.py` · `*_resolver.py` · `universal_downloader.py` (table-driven dispatch)

### ③ Lease + CAS: exactly-once processing

> **Problem**: The two classic concurrency failures — two coroutines processing the same page (duplicate exports), or a result written after another worker has already claimed the work (corrupted state).

- **Implementation**: Claiming work writes a lease (`leased_at` / `lease_expires`) renewed by a 60-second heartbeat. **Before anything is persisted, the worker must win a CAS** (compare-and-swap); losing means writing nothing at all. Watchdog and failure paths carry a "never overwrite completed" guard.
- **Key figures**: 60 s heartbeat · 300 s per-page watchdog (configurable, can be disabled) · tri-state CAS (won / lost / DB failure)
- **Modules**: `frontier.py` · `page_processor.py`

### ④ Watchdog and two-level rate limiting: run stability

> **Problem**: A single hung page can stall an entire batch. Naive rate limiting turns into a retry storm that never terminates.

- **Implementation**: A page that exceeds its timeout is killed and marked for retry without dragging down its batch. Rate limiting is a **two-level token bucket** (global + per-domain). Being throttled **does not consume retry budget** — it uses a separate counter with its own dead-letter threshold, so the main loop's exit condition can actually be reached.
- **Key figures**: watchdog **300 s** (normal page < 60 s, 5× headroom) · per-domain concurrency **20** · `Retry-After` capped at **300 s**
- **Modules**: `crawler.py` · `rate_limiter.py` · `concurrency.py`

### ⑤ Resumable downloads without re-encoding

> **Problem**: An interrupted large download should not restart from zero, and re-encoding destroys the original quality.

- **Implementation**: Probe total size and Range support first → stream into a `.part` file → resume from the exact byte offset → verify the byte count → **atomic rename** so nothing downstream ever sees a partial file. Video is byte-transported with no re-encoding; audio is converted to MP3 only when the audio channel is enabled. If the server does not support Range, the stale `.part` is discarded rather than concatenated into a corrupt file.
- **Key figures**: checked byte counts + atomic placement · m3u8 fragment support with AES-128 decryption · ffmpeg used only to **merge**, never to re-encode
- **Modules**: `universal_downloader.py` · `m3u8_downloader.py` · `media_downloader.py`

### ⑥ Hop-by-hop SSRF gate

> **Problem**: **Entry-point URL validation alone is not sufficient.** An external image URL can return a 302 pointing at an internal address, and the underlying HTTP library will follow it — **the internal response is then written to disk as an ordinary image.** Reproduced locally with two loopback services.

- **Implementation**: Every outbound fetch goes through one shared primitive — validate at the entry, disable automatic redirects, then **follow and re-validate hop by hop**. Hostnames are normalised first (IPv6 zone ids and trailing-dot FQDNs both used to slip past the blocklist). Private-network detection covers the ranges the standard library misses. DNS verdicts carry a 90-second expiry, defeating the "resolve to public, then repoint to internal" rebinding trick.
- **Key figures**: redirect cap **10 hops** · DNS verdict TTL **90 s** · 25 private-address forms covered · decimal, hex and octal IP variants handled
- **Modules**: `url_utils.py` (the highest fan-in module in the repo) · every download and discovery path

### ⑦ Three-layer redaction and its explicit boundaries

> **Problem**: Phone numbers, emails and tokens inside URLs leaking into results is a real risk. **Redaction also has boundaries** — applied in the wrong place it breaks functionality outright.

- **Implementation**: Three layers — logging (the filter attaches to the **handler**; attaching to the root logger silently does nothing), side channels (print bridge, GUI log, exception echo), and artifacts (exported copies are scrubbed record-wide).
  **The exclusion is explicit**: URLs inside the task queue are keys for a *later* request — redacting their signature parameters makes re-crawling fail with 403. That reasoning lives in the code comments so it is not "helpfully" broken later.
- **Key figures**: **19** sensitive URL parameter classes · phone / email / IP · record-level recursion to depth 6
- **Modules**: `sanitizer.py` · `config.py` (wiring) · `page_processor.py` (export copies)

### ⑧ Ten-check release gate

> **Problem**: In a single-maintainer project, the usual cause of a broken release is an assumption that went unchecked.

- **Implementation**: One command runs ten checks, and **any failure blocks the release**: version consistency across four files · clean git tree · full test suite · static analysis (baseline locked, may only go down) · dependency audit · secret scan · credential hygiene · rule and asset integrity · live multi-site regression · build surface.
- **Key figures**: **10 checks** · **675 offline test cases** · static baselines pinned to current counts (they may only go down)
- **Modules**: `tools/release_check.py` · `.github/workflows/ci.yml` · `tests/`

---

## Architecture

```
launcher_v9.py          GUI entry (five pages: home / logs / data / tasks / settings)
  └ launcher_v8.py      only 4 live symbols (engine bridge, log bridges, config path, secrets)
run_crawler.py          CLI entry — also the GUI's in-process engine
  └ crawler.py          engine assembly (7 _init_* = wiring table, run() = main loop)
      ├ engine_router.py      five-tier fallback scheduling
      ├ page_processor.py     single-page pipeline
      ├ frontier.py           SQLite queue (lease / CAS / migrations)
      └ url_utils.py          security primitives (highest fan-in)
```

**The journey of one page** (17 steps):

```
create task dir → seed routing → enqueue → claim batch (lease) → pick exit & slot → heartbeat
  → cache hit? → route (5 tiers) → parse-bomb guard → redact → dedup → quality gate
  → CAS claim → persist/export → media enqueue → link discovery → stats
```

**Runtime shape**: single process, asyncio coroutines; threads exist only to offload blocking calls — **no multiprocessing**. The engine is **not a subprocess**: the GUI runs it inside a daemon thread with its own event loop and bridges `print` and `logging` back to the UI.

---

## Quick start

**Requirements**: Windows 10/11 x64 · Python ≥ 3.11

```bash
# 1. install dependencies (runtime + dev, all pinned)
pip install -r kiana_vnext_plus/requirements.txt
pip install -r requirements-dev.txt

# 2. launch the desktop UI
python launcher_v9.py

# 3. or drive it from the command line
python run_crawler.py "https://example.com" -d 0 -m 1 -o ./out
python run_crawler.py -f urls.txt -d 3 -m 500 -o ./out

# 4. run the test suite (offline)
python -m pytest tests -q

# 5. run the release gate (10 checks)
python tools/release_check.py
```

Common flags: `-d` depth (`0` = seed only) · `-m` page cap · `-o` output dir · `--strategy bfs|dfs|bff` · `--page-timeout` per-page timeout (`0` = off) · `--robots` enable robots.txt compliance.

**Adding a site** (no code): write `rules/sites/<domain>.yaml` → validate with `python -m kiana_vnext_plus.cli rule-validate rules/sites/<domain>.yaml` → dry-run with `rule-test <file> --url <url>`.

---

## Boundaries & honest notes

Known limitations are stated explicitly, including the ones that remain open.

**Where its edges actually are**

- **It does handle anti-bot measures on public pages**: TLS impersonation, a real-browser fallback, challenge solving, optional CAPTCHA-service keys. That is a core capability — it is documented in highlight ① above rather than omitted.
- **It does not touch account permissions**: login walls, paywalls and account-scoped content are reached only with cookies *you* supply; when that is not enough it reports a readable error instead of forcing its way through.
- **Not a framework, not a service, not multi-user.** It is designed for one person on one machine.
- **No personal data collection.** Contact details, emails and IPs are redacted before anything is written.

**What it doesn't do yet (stated plainly)**

- Some platform resolvers are **verified only by offline tests**; there is no end-to-end evidence against the live service. Without a login session they degrade honestly — a readable reason instead of a forced workaround.
- A few live-site resolvers currently fail. The cause has not been isolated, so this is an open item rather than a proven platform-side change.
- The installer is large (~542 MB) — browser binaries ship inside, buying "install and run" with no runtime download.
- Coverage is reported but not enforced as a threshold; LLM-assisted features are off by default.

**In one line**: it reliably collects publicly accessible content; it is not a tool for reaching content behind account permissions or payment.

---

## License & provenance

- **This project's license**: **GPL-3.0** (see [`LICENSE`](LICENSE)). Not a preference — the project depends on GPLv3 components (below), and GPL-3.0 is the license consistent with shipping them.
- **Code provenance**: every source file here is this project's own implementation; the algorithm layer for platform APIs and network protocols follows public specification. See [`docs/来源与合规声明.md`](docs/来源与合规声明.md).
- **Dependency licenses (includes copyleft)**: `PySide6-Fluent-Widgets` **GPL-3.0** · `bgutil-ytdlp-pot-provider` **GPL-3.0** · `PySide6` LGPL/GPL (tri-licensed) — all redistributed inside the packaged build; every other dependency is MIT / BSD / PSF / Apache-2.0.
- **Bundled fonts**: Roboto (Apache-2.0) · Mochiy Pop One / Yusei Magic (**SIL OFL 1.1**, license text shipped under `assets/fonts/`).
- **Bundled illustrations**: three images (`assets/cover.png`, `tests/assets/sample_anime_*.{png,jpg}`) come from the author's personal collection — **not original work of this project**; they are used only for decoration and as test input. See [`docs/ASSET-PROVENANCE.md`](docs/ASSET-PROVENANCE.md).
