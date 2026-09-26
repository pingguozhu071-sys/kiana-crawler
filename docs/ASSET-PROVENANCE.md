# Image Asset Provenance

This document states the origin and intended use of the **decorative and test images** that ship
with this repository. It is kept as a separate, standalone file so that the provenance of these
assets is clear at a glance.

## 1. Which files this covers

| File | Purpose | Referenced by |
|---|---|---|
| `assets/cover.png` | Cover image for the installer and the repository | `kiana_setup.nsi` |
| `tests/assets/sample_anime_1.png` | Sample image for UI / background rendering tests | test asset directory |
| `tests/assets/sample_anime_2.jpg` | Input image for the wallpaper pipeline smoke test | `tools/v9_smoke.py` |

## 2. Origin and nature

- These three images are **not original work of this project**. They come from the **author's
  personal collection** — anime-style illustrations chosen out of personal preference and
  included with the repository **only for the decorative and testing purposes listed above**.
- They **play no part in any functional logic**. They are not used by crawling, parsing,
  redaction, or downloading, and they do not affect the behavior of the program in any way.
  The test case simply treats one of them as "any local image file" in order to exercise the
  wallpaper pipeline's processing and rendering paths.
- The images **do not depict any real person** and are unrelated to any actual individual.
- The **original artist and license terms could not be traced**. That is precisely why this
  document exists: to state the situation plainly rather than gloss over it.

## 3. If you believe an image should not be here

Please open an issue or contact the repository author directly. The image **will be removed
immediately, with no conditions attached**.

The same applies to any asset in this repository that you believe raises a rights concern.
`docs/来源与合规声明.md` documents this project's general policy toward third-party resources,
and this file is how that policy is applied to the image assets.

---

> Note: the license status of the repository's **other** image assets (`assets/icon.ico`,
> installer artwork) and of the bundled fonts (`assets/fonts/`) is listed in the resource table
> of `docs/来源与合规声明.md`.
