"""Build and validate the bridge locally on Linux or macOS, without GitHub Actions.

Run with the Python environment containing requirements-dev.txt:
    .venv/bin/python tools/test_posix.py

Requires CMake, a C compiler, and JDK 17+. The only permitted skips are the PowerShell
publication tool tests, and only when PowerShell 7 (pwsh) is not installed.
"""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
# The publication tests run wherever PowerShell 7 is installed; without it they are the only
# skips this gate accepts.
PWSH_SKIP_REASON = "PowerShell 7 (pwsh) is required for the publication tool tests"
PUBLISH_TESTS = "test_publish_release.TestPublishRelease"


def _publish_test(item):
    if item.get("id"):
        return item["id"].startswith(PUBLISH_TESTS + ".")
    name, _, owner = item["test"].partition(" (")
    return owner in (f"{PUBLISH_TESTS}.{name})", f"{PUBLISH_TESTS})")


def validate_test_report(report):
    unexpected = [item for item in report["skipped"]
                  if item["reason"] != PWSH_SKIP_REASON or not _publish_test(item)]
    if (not report["passed"] or not report["testsRun"] or report["failures"]
            or report["errors"] or unexpected):
        raise RuntimeError(f"Tests failed or unexpectedly skipped: {unexpected}")


def java_home(system):
    configured = os.environ.get("JAVA_HOME")
    if configured:
        home = Path(configured)
    elif system == "Darwin":
        home = Path(subprocess.check_output(["/usr/libexec/java_home", "-v", "17"], text=True).strip())
    else:
        home = Path(shutil.which("javac")).resolve().parent.parent
    if not (home / "include" / "jni.h").is_file():
        raise RuntimeError("JAVA_HOME must point to a complete JDK 17+ installation")
    return home


def run(command, environment):
    subprocess.run([str(value) for value in command], cwd=ROOT, env=environment,
                   check=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--report", type=Path, default=ROOT / "dist" / "posix-test-report.json")
    args = parser.parse_args(argv)
    system = platform.system()
    if system not in ("Linux", "Darwin"):
        parser.error("Use tools/Test-Local.ps1 on Windows")
    try:
        # Invalidate a previous successful run even if preflight/build fails.
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps({"passed": False, "posixValidationPassed": False,
                                          "platform": system}, indent=2) + "\n")
        for executable in ("cmake", "cc", "javac", "java"):
            if not shutil.which(executable):
                raise RuntimeError(f"Missing {executable}; install CMake, C build tools, and JDK 17+")
        for dependency in ("pyModeS", "reedsolo", "yaml"):
            if importlib.util.find_spec(dependency) is None:
                raise RuntimeError("Install requirements-dev.txt in this Python environment first")
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        home = java_home(system)
        environment["JAVA_HOME"] = str(home)
        environment["PATH"] = str(home / "bin") + os.pathsep + environment["PATH"]
        version = subprocess.run([str(home / "bin" / "javac"), "-version"], capture_output=True,
                                 text=True, check=True)
        major = re.search(r"javac (\d+)", version.stdout + version.stderr)
        if not major or int(major.group(1)) < 17:
            raise RuntimeError("Desktop JNI replay requires JDK 17 or newer")

        build = ROOT / "build" / f"{system.lower()}-{platform.machine()}"
        build.mkdir(parents=True, exist_ok=True)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        # Keep Java and test scratch files on this host's native temporary filesystem.
        run(["cmake", "-S", ROOT, "-B", build, "-DCMAKE_BUILD_TYPE=Release", "-DSB_FUZZ_ASAN=ON"], environment)
        run(["cmake", "--build", build, "--parallel", "2"], environment)
        suffix = ".dylib" if system == "Darwin" else ".so"
        libraries = {"SBCORE_LIB": build / ("libsbcore" + suffix),
                     "SENTRY_JNI_LIB": build / ("libsbjni" + suffix),
                     "SENTRY_BRIDGE_BIN": build / "sentry-bridge"}
        for name, path in libraries.items():
            if not path.is_file():
                raise RuntimeError(f"Required native build missing: {path}")
            environment[name] = str(path)
        run([sys.executable, ROOT / "tools" / "run_tests.py", "--report", args.report,
             "--allow-skips"], environment)
        report = json.loads(args.report.read_text())
        try:
            validate_test_report(report)
        except RuntimeError:
            report["passed"] = False
            args.report.write_text(json.dumps(report, indent=2) + "\n")
            raise
        # Record partial validation before the remaining gates so a failure cannot
        # leave a report claiming the complete POSIX check succeeded.
        report["posixValidationPassed"] = False
        args.report.write_text(json.dumps(report, indent=2) + "\n")
        run([sys.executable, ROOT / "tools" / "smoke_bridge.py", libraries["SENTRY_BRIDGE_BIN"]], environment)
        environment["ASAN_OPTIONS"] = "halt_on_error=1"
        environment["UBSAN_OPTIONS"] = "halt_on_error=1:print_stacktrace=1"
        run([build / "fuzz-core", "100000"], environment)
        report.update(posixValidationPassed=True, nativeSmokePassed=True,
                      sanitizer="AddressSanitizer and UndefinedBehaviorSanitizer",
                      fuzzIterations=100000, allowedPublicationSkips=len(report["skipped"]))
        args.report.write_text(json.dumps(report, indent=2) + "\n")
        print(f"PASS: {report['testsRun']} tests; {len(report['skipped'])} skips (publication tests without pwsh); "
              "native UDP smoke and 100,000 ASan/UBSan inputs")
        print(f"Report: {args.report}")
        return 0
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Local POSIX validation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
