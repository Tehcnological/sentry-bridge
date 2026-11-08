# Contributing

Thanks for helping. Bug reports and device reports are welcome. Pull requests are welcome too, with
one condition on copyright below. Everyone taking part follows the
[code of conduct](CODE_OF_CONDUCT.md).

## Copyright

Sentry Bridge is published under GPL-3.0-only, with the additional permission in
[NOTICE](NOTICE.txt). Christopher DeFoe keeps sole copyright so he can publish it in app stores
under other terms.

That means outside pull requests can only be accepted from authors who first assign the copyright
in their change to Christopher DeFoe. Without that, please open an issue describing the change
instead of a pull request.

## Reporting a problem

[Open an issue](https://github.com/Tehcnological/sentry-bridge/issues) with:

- the Sentry Bridge version and platform (Windows, Android, iPhone or iPad), and the OS version
- the receiver and the EFB app you use
- what you did, what you expected, and what happened instead

Before attaching logs or captures, remove anything personal, such as tail numbers, your location,
or WiFi names. Report security problems privately instead; see [SECURITY.md](SECURITY.md).

Reports from real hardware help the most: which receivers, tablets and EFB apps work, and which
do not. See [Compatibility](docs/Compatibility.md).

## Pull requests

1. Outside contributors: assign the copyright in your change first (see [Copyright](#copyright)).
2. Fork the repository and branch from `master`.
3. Keep one change per pull request, with tests and the docs it affects.
4. Run the tests locally (below), then open the pull request against `master`.
5. In the description, say what changed and how you checked it. Mention anything you could only
   check on a device, and anything you could not check. The pull request template lists the checks.

The **CI** check must pass before a merge. It builds and tests every platform; see
[Continuous integration](docs/CI.md). A first-time contributor's run starts after a maintainer
approves it.

## Running the tests

```bash
pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

For the full local check, run `tools/test_posix.py` on Linux or macOS, or `tools\Test-Local.ps1` on
Windows. Both fail on any skipped test, as CI does. [Building from source](docs/Building.md) lists
what each test needs and how to build each platform.

## Ground rules

- **Tests check real behaviour.** Add a test for each fix or feature, and make sure it fails without
  your change. Do not loosen or skip a test to make it pass: a failing test that tells the truth is
  better than a passing one that does not. If a test is wrong, say why in the pull request.
- **The decoders must agree.** `bridge.py` is the Python reference. The C core (`core/`) serves the
  Windows bridge, the desktop app, Android and iOS. Their output must be identical byte for byte,
  and the parity tests check that. A decoding or encoding change goes into both.
- **Write your own code.** Do not copy code from other ADS-B or UAT decoders, or from any
  receiver's firmware.
- **Generated files stay generated.** The Xcode project comes from `tools/generate_ios_project.py`;
  change the generator, not the project. CI checks that the project is current.
- **No personal data.** Do not commit real traffic captures, locations, tail numbers, serial
  numbers, WiFi names or credentials. Tests use simulated traffic.
- **Supported platforms.** Windows, Android, iPhone and iPad are maintained. macOS and Linux are
  unmaintained: the desktop app may run there from source, and changes for them are welcome if
  they keep the tests passing.
