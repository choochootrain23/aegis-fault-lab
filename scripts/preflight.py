"""Read-only runtime admission check. Does not install or reconfigure anything."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
failures = []
memory = next(int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:'))
free = shutil.disk_usage(root).free
print(f'Available memory: {memory/1024**3:.2f} GiB (startup target: 2 GiB)')
print(f'Free project filesystem: {free/1024**3:.1f} GiB (minimum: 1 GiB, for exports and reports)')
if memory < 2*1024**3: failures.append('Insufficient available memory for startup target')
if free < 1024**3: failures.append('Insufficient free disk for the project folder')
try:
    result = subprocess.run(['docker', 'info', '--format', '{{json .}}'], capture_output=True, text=True, timeout=10)
    if result.returncode:
        failures.append('Docker daemon inaccessible. Use a terminal with existing authorized Docker access; do not chmod the socket.')
    else:
        info = json.loads(result.stdout)
        print('Docker server:', info.get('ServerVersion'))
        # Images, containers and the evidence volume live in Docker's own storage, which may be another disk.
        docker_root = info.get('DockerRootDir')
        try:
            docker_free = shutil.disk_usage(docker_root).free
            print(f'Free Docker storage ({docker_root}): {docker_free/1024**3:.1f} GiB (minimum: 10 GiB)')
            if docker_free < 10*1024**3: failures.append('Insufficient free disk for Docker images and volumes')
        except (OSError, TypeError):
            print('Free Docker storage: could not be read; make sure about 10 GiB is free where Docker keeps its data')
        for field in ('MemoryLimit', 'SwapLimit', 'CpuCfsQuota', 'PidsLimit'):
            if not info.get(field): failures.append(f'Docker reports missing {field} support')
        print('Daemon resource-control support checked; effective container settings still require live validation.')
except (OSError, subprocess.TimeoutExpired, ValueError):
    failures.append('Docker check could not complete')
if failures:
    for issue in failures: print('BLOCKED:', issue)
    sys.exit(1)
print('Preflight passed. This does not replace container isolation or recovery validation.')
