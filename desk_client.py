"""Field reporter tool - works fully offline. Send the output by any channel (email, USB, satellite).
  DESK_ID=sylhet DESK_KEY=<fernet key> python desk_client.py story.json [photo.jpg] > story.dmpkg
story.json: {"slug","category","title","summary","body","author"}.  Generate a key: Fernet.generate_key()."""
import base64, hashlib, json, os, sys, uuid
from cryptography.fernet import Fernet

desk, key = os.environ["DESK_ID"], os.environ["DESK_KEY"]
payload = {"desk": desk, "nonce": uuid.uuid4().hex, "article": json.load(open(sys.argv[1], encoding="utf-8"))}
if len(sys.argv) > 2:
    img = open(sys.argv[2], "rb").read()
    payload["image_b64"] = base64.b64encode(img).decode()
    payload["image_sha256"] = hashlib.sha256(img).hexdigest()
print(f"{desk}.{Fernet(key.encode()).encrypt(json.dumps(payload).encode()).decode()}")
