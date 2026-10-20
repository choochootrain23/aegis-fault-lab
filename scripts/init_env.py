"""Create local lab credentials without printing them or replacing existing ones.

Run again after upgrading: it only appends credentials that .env does not have yet.
"""
import os
import secrets
from pathlib import Path

KEYS = ('AEGIS_BROKER_TOKEN', 'AEGIS_FLEET_TOKEN')
path = Path(__file__).resolve().parents[1] / '.env'
fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
with os.fdopen(fd, 'r+') as f:
    f.seek(0)
    present = {line.split('=', 1)[0] for line in f.read().splitlines() if '=' in line}
    missing = [key for key in KEYS if key not in present]
    for key in missing:
        f.write(f'{key}={secrets.token_hex(32)}\n')
os.chmod(path, 0o600)
print(f'Added {", ".join(missing)} to .env. Keep it out of source control.' if missing else '.env already has every credential; nothing changed.')
