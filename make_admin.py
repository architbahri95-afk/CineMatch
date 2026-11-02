"""Grant or remove dashboard access:   python make_admin.py USERNAME        (add --remove to revoke)

The first real account created is an admin automatically; use this to add more.
You can also set the ADMIN_USERS environment variable (comma-separated usernames)."""
import sys
from pathlib import Path

from app import db

args = [a for a in sys.argv[1:] if not a.startswith("--")]
if len(args) != 1:
    sys.exit(__doc__)
conn = db.connect(str(Path(__file__).parent / "cinematch.db"))
row = conn.execute("SELECT id FROM users WHERE name = ? COLLATE NOCASE AND is_demo = 0", (args[0],)).fetchone()
if not row:
    sys.exit(f"No account named {args[0]!r}.")
conn.execute("UPDATE users SET is_admin = ? WHERE id = ?", (0 if "--remove" in sys.argv else 1, row["id"]))
conn.commit()
print(f"{args[0]} is {'no longer' if '--remove' in sys.argv else 'now'} an admin.")
