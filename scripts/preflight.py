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
print(f'Free project filesystem: {free/1024**3:.1f} GiB (minimum: 10 GiB)')
if memory < 2*1024**3: failures.append('Insufficient available memory for startup target')
if free < 10*1024**3: failures.append('Insufficient free disk')
try:
    result = subprocess.run(['docker', 'info', '--format', '{{json .}}'], capture_output=True, text=True, timeout=10)
    if result.returncode:
        failures.append('Docker daemon inaccessible. Use a terminal with existing authorized Docker access; do not chmod the socket.')
    else:
        info = json.loads(result.stdout)
        print('Docker server:', info.get('ServerVersion'))
        for field in ('MemoryLimit', 'SwapLimit', 'CpuCfsQuota', 'PidsLimit'):
            if not info.get(field): failures.append(f'Docker reports missing {field} support')
        print('Daemon resource-control support checked; effective container settings still require live validation.')
except (OSError, subprocess.TimeoutExpired, ValueError):
    failures.append('Docker check could not complete')
if failures:
    for issue in failures: print('BLOCKED:', issue)
    sys.exit(1)
print('Preflight passed. This does not replace container isolation or recovery validation.')
