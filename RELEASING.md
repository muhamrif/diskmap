# Releasing diskmap

Friends install the ready-made apps attached to the latest GitHub Release. GitHub builds them for you: you push a
version tag, it builds the Apple silicon and Intel apps on a real Mac, tests them, and publishes the release.

## Publish a new version

```bash
cd ~/projects/diskmap
echo 1.0.1 > VERSION                       # the new version number
git add -A && git commit -m "diskmap 1.0.1"
git push
git tag v1.0.1 && git push origin v1.0.1   # this starts the release build
```

Watch it under the repo's **Actions** tab (about 10 minutes). When it's green, the release is live and everyone's
diskmap shows "diskmap 1.0.1 is available" within a day. If the build fails, nothing is published; fix it, then move
the tag: `git tag -f v1.0.1 && git push -f origin v1.0.1`.

Every push to `main` also runs the full build and tests without publishing, so you'll know before you tag.

## What the build checks

- **On Linux:** Python compiles on 3.9 (the oldest it supports); the JavaScript in every page parses; shellcheck on
  all scripts; the installer against simulated Macs (Apple silicon, Intel, Rosetta, old macOS, offline, tampered
  download, no release yet, updating a running copy, low disk space, uninstall); a dashboard smoke test.
- **On a real Mac (macos-15):** builds both apps with Python built in; runs the dashboard with the bundled Python in
  demo and real mode, calling every API and failing on any Python error; proves it never launches Apple's placeholder
  developer tools (on a Mac without them those pop up an install dialog); checks nothing is written inside the app;
  checks the Intel app really is Intel; verifies code signatures; installs from the zip like a friend would, launches
  it, updates it while running and uninstalls it.

## Test locally before tagging

```bash
bash macapp/build.sh                # build + install your own copy (Python built in, like friends get)
bash macapp/build.sh --release      # build both release zips into dist/ without publishing
bash install.sh --from-zip dist/diskmap-macos-arm64.zip    # install a zip exactly the way friends do
bash tests/smoke.sh                 # server end-to-end test
bash tests/test_install.sh          # installer scenarios
```

## How the pieces fit

| File | Job |
|---|---|
| `install.sh` | What friends run. Picks Apple silicon or Intel, downloads the zip from the latest release, checks its SHA-256, quits a running copy, installs to `~/Applications`, opens it. `--uninstall`, `--purge`, `--version X`, `--from-zip F`, `--source`. |
| `macapp/build.sh` | Builds the app for one CPU (`--arch`), or both zips plus `SHA256SUMS.txt` (`--release`). |
| `macapp/fetch-python.sh` | Downloads a pinned, checksum-verified [python-build-standalone](https://github.com/astral-sh/python-build-standalone) CPython, trims it and precompiles it into the app. |
| `.github/workflows/build.yml` | Checks, the Mac build and tests, and publishing on `v*` tags. |
| `VERSION` | The version stamped into the app; the app compares it with the latest release to offer updates. |

### Updating the bundled Python

Python is pinned to 3.12.15 (python-build-standalone release `20261009`). To move to a newer build, pick a release at
least a couple of weeks old, then in `macapp/fetch-python.sh` change `PBS_TAG`, `PY_VERSION` and the two `SHA_…`
values (from that release's `SHA256SUMS` file, the `aarch64-apple-darwin` and `x86_64-apple-darwin`
`install_only_stripped` lines). Push to `main` and let CI confirm it before tagging.

## Notes

- The apps are signed "ad-hoc" (no paid Apple Developer account). That's why the Terminal installer is the recommended
  way: files downloaded by `curl` aren't quarantined, so macOS opens the app without the "unidentified developer" block.
  Downloading the zip in a browser works too, but needs **Open Anyway** in Privacy & Security after each update.
- Because ad-hoc signatures change with every build, macOS may ask for Full Disk Access again after an update.
  A paid Apple Developer ID ($99/year) would fix both, and the build would just need `codesign` with that identity plus
  notarization.
- There's no license file, which means others can look at the code but not reuse it. Add one (MIT is common) if you
  want people to be able to.
