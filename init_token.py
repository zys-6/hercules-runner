"""Create a private local shared token without displaying it."""
from pathlib import Path
import os
import secrets
root = Path(__file__).resolve().parent / 'data'
root.mkdir(mode=0o700, exist_ok=True)
os.chmod(root, 0o700)
path = root / 'service.token'
try:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    pass
else:
    with os.fdopen(fd, 'w') as handle:
        handle.write(secrets.token_urlsafe(32))
