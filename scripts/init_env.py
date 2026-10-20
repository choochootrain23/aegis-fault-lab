"""Create local broker credentials without printing them or overwriting existing ones."""
import os
import secrets
from pathlib import Path
path = Path(__file__).resolve().parents[1] / '.env'
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as f:
    f.write('AEGIS_BROKER_TOKEN=' + secrets.token_hex(32) + '\n')
print('Created .env with a local broker credential. Keep it out of source control.')
