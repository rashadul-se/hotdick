"""Generate Render environment values. Standard library only.
  python gen_env.py                 -> one desk called "newsroom"
  python gen_env.py dhaka sylhet    -> one desk (own key) per name
Keep the output private; never commit it to Git."""
import base64, json, os, re, sys

names = sys.argv[1:] or ["newsroom"]
cfg = {}
for n in names:
    if not re.fullmatch(r"[a-z0-9-]{2,30}", n):
        sys.exit(f"Bad desk name '{n}': use lowercase letters, digits, hyphens")
    cfg[n] = {"key": base64.urlsafe_b64encode(os.urandom(32)).decode(),
              "scopes": ["publish:*"], "ttl": 172800}

print("DESK_KEYS  (paste into Render, one line):")
print(json.dumps(cfg, separators=(",", ":")))
print("\nFor each reporter's desk_client.py:")
for n, c in cfg.items():
    print(f"  DESK_ID={n}  DESK_KEY={c['key']}")
print("\nPUBLIC_URL      : https://<your-service-name>.onrender.com (or your custom domain)")
print("ADSENSE_CLIENT  : leave blank to keep ads off")
