"""Read hardware, verify a chosen Python's accelerators, and check MinerU updates.

No installation or configuration changes. Run before selecting an environment
and again after installation. PyPI failure is reported as unknown, never current.
"""
from __future__ import annotations

import argparse
import csv
import ctypes
import io
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone

PROBE = r'''
import importlib.metadata as m, json, platform, sys
result = {"executable": sys.executable, "version": platform.python_version(), "packages": {}}
for name in ["mineru", "torch", "torchvision", "pypdfium2", "pillow", "lmdeploy", "transformers"]:
    try: result["packages"][name] = m.version(name)
    except m.PackageNotFoundError: result["packages"][name] = None
result["accelerator"] = {"cuda_available": False, "cuda_test_passed": False, "mps_test_passed": False}
try:
    import torch
    a = result["accelerator"]
    a["torch_cuda"] = torch.version.cuda
    a["cuda_available"] = torch.cuda.is_available()
    a["devices"] = []
    if a["cuda_available"]:
        for i in range(torch.cuda.device_count()):
            d = {"index": i, "name": torch.cuda.get_device_name(i), "capability": list(torch.cuda.get_device_capability(i))}
            try:
                x = torch.ones(2, device=f"cuda:{i}")
                d["test_passed"] = (x + 1).sum().item() == 4
                free, total = torch.cuda.mem_get_info(i)
                d.update(free_mib=round(free/2**20), total_mib=round(total/2**20))
                del x
            except Exception as exc: d["error"] = str(exc)
            a["devices"].append(d)
        a["cuda_test_passed"] = any(d.get("test_passed") for d in a["devices"])
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        x = torch.ones(2, device="mps")
        a["mps_test_passed"] = (x + 1).sum().item() == 4
except Exception as exc:
    result["accelerator"]["error"] = str(exc)
print("MINERU_PROBE=" + json.dumps(result, ensure_ascii=True))
'''


def command(args: list[str], timeout: int = 45) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, check=False)


def memory_bytes() -> int | None:
    try:
        if platform.system() == "Windows":
            class MemoryStatus(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
                    (name, ctypes.c_ulonglong) for name in
                    ("total_phys", "avail_phys", "total_page", "avail_page", "total_virtual", "avail_virtual", "extended")]
            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return status.total_phys
        if platform.system() == "Darwin":
            return int(command(["sysctl", "-n", "hw.memsize"]).stdout.strip())
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
        return None


def inspect(python: str, disk_path: Path, offline: bool = False) -> dict:
    result = {"checked_at": datetime.now(timezone.utc).isoformat(),
              "system": platform.system(), "release": platform.release(), "machine": platform.machine(),
              "cpu_count": os.cpu_count(), "ram_bytes": memory_bytes(),
              "disk_free_bytes": shutil.disk_usage(disk_path).free, "nvidia": []}
    smi = shutil.which("nvidia-smi")
    if smi:
        try:
            check = command([smi, "--query-gpu=name,memory.total,memory.free,driver_version", "--format=csv,noheader,nounits"])
            if check.returncode == 0:
                result["nvidia"] = [dict(zip(("name", "total_mib", "free_mib", "driver"), map(str.strip, row)))
                                    for row in csv.reader(io.StringIO(check.stdout))]
            else:
                result["nvidia_error"] = check.stderr.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            result["nvidia_error"] = str(exc)
    try:
        check = command([python, "-c", PROBE], timeout=60)
        payload = next(line.removeprefix("MINERU_PROBE=") for line in check.stdout.splitlines()
                       if line.startswith("MINERU_PROBE="))
        result["python"] = json.loads(payload)
    except (OSError, StopIteration, ValueError, subprocess.SubprocessError) as exc:
        result["python"] = {"executable": python, "error": str(exc) or "Python probe failed"}
    update = {"status": "offline" if offline else "unknown", "latest": None,
              "installed": result["python"].get("packages", {}).get("mineru")}
    if not offline:
        try:
            with urllib.request.urlopen("https://pypi.org/pypi/mineru/json", timeout=20) as response:
                info = json.load(response)["info"]
            update.update(latest=info["version"], requires_python=info["requires_python"], source="https://pypi.org/project/mineru/")
            if not update["installed"]:
                update["status"] = "not_installed"
            elif update["installed"] == update["latest"]:
                update["status"] = "current"
            else:
                update["status"] = "different_version_check_compatibility"
        except (OSError, ValueError, KeyError) as exc:
            update["error"] = str(exc)
    result["mineru_update"] = update
    accelerator = result["python"].get("accelerator", {})
    device, backend, reason = "cpu", "pipeline", "No verified local accelerator"
    ready = [d for d in accelerator.get("devices", []) if d.get("test_passed")]
    if ready:
        best = max(ready, key=lambda d: d.get("free_mib", 0))
        device = f"cuda:{best['index']}"
        # Nominal 8 GB laptop GPUs can expose slightly less than 8192 MiB.
        if best.get("total_mib", 0) >= 7500 and best.get("free_mib", 0) >= 6000:
            backend, reason = "hybrid-engine", "Nominal 8 GB or larger GPU; verify on a small PDF before a full run"
        else:
            reason = "CUDA works; prefer the lower-memory pipeline and verify available VRAM"
    elif accelerator.get("mps_test_passed"):
        device, reason = "mps", "MPS verified; begin with pipeline and consult current Apple Silicon guidance for VLM"
    elif result["nvidia"]:
        reason = "NVIDIA hardware found but CUDA is not verified; repair the target Python's CUDA packages before considering CPU fallback"
    result["recommendation"] = {"device": device, "backend": backend, "reason": reason,
                                 "requires_smoke_test": True}
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=sys.executable, help="Python interpreter used for MinerU")
    parser.add_argument("--disk-path", type=Path, default=Path.cwd(), help="Existing directory on intended installation/output disk")
    parser.add_argument("--output", type=Path, help="Save the same JSON report to this path")
    parser.add_argument("--offline", action="store_true", help="Explicitly skip the PyPI version check")
    args = parser.parse_args()
    report = inspect(args.python, args.disk_path.resolve(strict=True), args.offline)
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
