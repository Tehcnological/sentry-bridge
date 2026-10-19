"""Build the microcontroller firmware into dist/ (Linux or WSL):

  sentry-bridge-esp32c3.bin   ESP32-C3, one image to write at offset 0x0
  sentry-bridge-pico-w.uf2    Raspberry Pi Pico W
  sentry-bridge-pico2-w.uf2   Raspberry Pi Pico 2 W

and dist/firmware-report.json with the sources and hashes. Releases do not include the images yet.
Toolchains are read from --tools (default ~/fw):
  esp-idf/ (v5.4.1) with espressif/ (its tools), pico-sdk/ (2.1.1) and arm-gnu/ (Arm GNU 14.2).
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import release_bundle  # noqa: E402

FIRMWARE = {"sentry-bridge-esp32c3.bin": "esp32c3", "sentry-bridge-pico-w.uf2": "pico_w",
            "sentry-bridge-pico2-w.uf2": "pico2_w"}


def run(cmd, env=None, cwd=None):
    result = subprocess.run(cmd, env=env, cwd=cwd, capture_output=True, text=True)
    if result.returncode:
        sys.exit(f"{' '.join(map(str, cmd))[:200]} failed:\n{(result.stdout + result.stderr)[-3000:]}")
    return result.stdout


def build_esp32c3(tools, src, out):
    env = dict(os.environ, IDF_TOOLS_PATH=str(tools / "espressif"))
    libusb = next((tools / "libusb").rglob("libusb-1.0.so.0"), None) if (tools / "libusb").is_dir() else None
    if libusb:  # only the debugger needs it, but ESP-IDF checks every installed tool
        env["LD_LIBRARY_PATH"] = str(libusb.parent)
    build = out / "esp32c3"
    script = (f". '{tools}/esp-idf/export.sh' >/dev/null && cd '{src}/firmware/esp32c3' && "
              f"idf.py -B '{build}' set-target esp32c3 >/dev/null && idf.py -B '{build}' build >/dev/null && "
              f"cd '{build}' && python -m esptool --chip esp32c3 merge_bin -o '{out}/esp32c3.bin' @flash_args >/dev/null")
    run(["bash", "-c", script], env=env)
    return out / "esp32c3.bin"


def build_pico(tools, src, out, board):
    # pico-sdk 2.1.1's host tools (pioasm) predate GCC 15, which no longer includes <cstdint> implicitly.
    env = dict(os.environ, PICO_SDK_PATH=str(tools / "pico-sdk"), CXXFLAGS="-include cstdint",
               PATH=f"{tools / 'arm-gnu' / 'bin'}:{os.environ['PATH']}")
    build = out / board
    run(["cmake", "-S", f"{src}/firmware/pico", "-B", str(build), "-G", "Unix Makefiles", f"-DPICO_BOARD={board}"], env=env)
    run(["cmake", "--build", str(build), f"-j{os.cpu_count() or 4}"], env=env)
    return build / "sentry_bridge.uf2"


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tools", type=Path, default=Path.home() / "fw")
    parser.add_argument("--dist", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    if sys.platform != "linux":
        sys.exit("Build the firmware on Linux or in WSL.")
    sources = release_bundle.source_snapshot(ROOT)
    args.dist.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sb-firmware-") as temp:
        temp = Path(temp)
        src = temp / "src"
        for part in ("firmware", "core"):  # build outside the checkout: Windows drives are slow under WSL
            shutil.copytree(ROOT / part, src / part)
        images = {"sentry-bridge-esp32c3.bin": build_esp32c3(args.tools, src, temp)}
        for name, board in FIRMWARE.items():
            if board != "esp32c3":
                images[name] = build_pico(args.tools, src, temp, board)
        hashes = {}
        for name, image in images.items():
            shutil.copyfile(image, args.dist / name)
            hashes[name] = sha256(args.dist / name)
    if release_bundle.source_snapshot(ROOT) != sources:
        sys.exit("Sources changed during the firmware build; build again.")
    report = {"sources": sources, "assets": hashes, "version": release_bundle.app_version(ROOT)}
    (args.dist / "firmware-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    for name, digest in hashes.items():
        print(f"{digest}  {name}")


if __name__ == "__main__":
    main()
