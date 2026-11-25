# Continuous integration

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs on every pull request to `master`, on
every push to `master`, and by hand from the Actions tab. It uses standard GitHub-hosted runners, which
are free for public repositories, and no secrets.

## Jobs

| Check | Runner | What it does |
|---|---|---|
| Lint | `ubuntu-latest` | Python syntax and pyflakes rules (ruff), C with warnings as errors (GCC) |
| Linux (Python 3.10, 3.13) | `ubuntu-latest` | `tools/test_posix.py`: native build, full test suite, UDP smoke test, 100,000 corruption inputs under ASan and UBSan |
| Windows | `windows-latest` | `tools/Test-Local.ps1`: MSVC build, full test suite, packaged control tool and GUI smoke tests, 100,000 corruption inputs under ASan |
| macOS | `macos-latest` | `tools/test_posix.py` |
| Android | `ubuntu-latest` | Unsigned release APK with the pinned SDK, NDK and Gradle; package name and JNI exports in all three ABIs; `tools/test_android_diagnostics.py` |
| iOS | `xcode-27` | Generated project is current; unsigned Sideload and Lite IPAs; adapter tests; Swift worker on the host; UI tests on the simulator (Sideload build; fails unless exactly the 7 Lite-only tests skip) |
| iOS Lite UI | `xcode-27` | UI tests on the simulator, Lite build, in parallel with iOS (fails unless exactly the 1 Sideload-only test skips) |
| CI | `ubuntu-latest` | Passes only if everything above passed or was skipped as docs-only |

No test may be skipped on any runner: `tools/ci_report.py` fails the job if one is, and shows failed
tests as annotations on the pull request.

## Keeping runs down

- A new push to a pull request cancels its previous run. Runs on `master` are not cancelled.
- A change touching only `docs/` or `*.md` files runs Lint and Linux and skips the rest. Linux stays
  because tests read some of the documents.

## Required check

Require the single check named **CI** in a ruleset on `master`, not the individual jobs: its name stays
fixed, and it passes when platforms are skipped, while a job that does not run would leave a required
check waiting forever. Do not require "up to date with the base branch": every push to `master` would
then rerun every open pull request.

Settings > Rules > Rulesets, on the default branch:

- Block force pushes, and restrict deletions
- Require status checks to pass: `CI`, from GitHub Actions
- Bypass list: Repository admin, so that documentation can still be pushed straight to `master`. An
  admin's merge is not stopped by a red check either, so read the check before merging.

## Reviews

Cursor Bugbot and OpenAI Codex run from their own GitHub apps and are independent of this workflow.
They are not required checks: Bugbot often reports a neutral result, and neither runs on a direct push.

## Security

- The workflow token can only read the repository, and checkouts do not keep it.
- The workflow uses `pull_request`, never `pull_request_target`, so code from a fork runs without
  secrets or write access. First-time contributors need approval before their runs start.
- Actions are pinned to commits. Dependabot proposes updates monthly. Gradle and the org.json test
  dependency are checked against SHA-256 hashes.

## Limits

- **Not tested here:** real hardware. Receiver traffic, Android background behavior and the app on a
  device still need the checks on real devices.
- **Signed releases stay local.** The signing key is not in Actions. CI builds an unsigned APK and
  unsigned IPAs, and keeps no artifacts. `tools/Validate-All.ps1` remains the release gate, and
  `tools/Publish-Release.ps1` refuses to publish if a workflow can start on a release or tag.
- **Small-board firmware is not built.** It needs ESP-IDF, pico-sdk and the Arm toolchain; see
  [Small-board firmware](Firmware.md). Its shared bridge loop is compiled and tested with the core.
- **Android lint is not a gate.** `abortOnError` is off because its target-SDK date rules would block
  builds of this sideloaded app.
- **`xcode-27` is a public preview image** and may change. `macos-latest` has only Xcode 26, and the app
  needs the iOS 27 SDK.
- **Time-sensitive tests** use real sockets and can be slower on shared runners. Rerun only after
  reading the failure.
