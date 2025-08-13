import os, time, json, tweepy
import sys
from datetime import datetime, timezone

CHECKPOINT_FILE = "mentions_since_id.json"
API_KEY              = os.getenv("X_API_KEY")
API_SECRET           = os.getenv("X_API_SECRET")
BEARER_TOKEN         = os.getenv("X_BEARER_TOKEN")
ACCESS_TOKEN         = os.getenv("X_ACCESS_TOKEN")            
ACCESS_TOKEN_SECRET  = os.getenv("X_ACCESS_TOKEN_SECRET")     

POLL_SECONDS = 30

ME_USERNAME = "SharanReddy1694"
me = client.get_user(username=ME_USERNAME, user_fields=["username","created_at"])
my_id = me.data.id
print(f"✅ Authenticated as @{me.data.username} (id={my_id})")

def analyze(text: str) -> str | None:
  
    short = text.replace("\n", " ")[:180]
    return f"Thanks for the mention! Quick take: {short}"

try:

    resp = client.get_users_mentions(
        id=my_id,
        max_results=5,
        tweet_fields=["created_at","author_id","conversation_id"],
        expansions=["author_id"],
        user_fields=["username","name"]
    )
    last = getattr(client, "get_last_response", None)
    if callable(last) and last():
        hdrs = last().headers
        limit = hdrs.get("x-rate-limit-limit")
        remaining = hdrs.get("x-rate-limit-remaining")
        reset = hdrs.get("x-rate-limit-reset")
        print(f"⏱️ rate limit={limit} remaining={remaining} reset_epoch={reset}")

    if not resp.data:
        print("ℹ No mentions found (yet). Make sure another account mentions you: @SharanReddy1694")
        raise SystemExit(0)

    latest = max(resp.data, key=lambda t: int(t.id))
    users = {u.id: u.username for u in (resp.includes.get("users") or [])}
    author = users.get(latest.author_id, "unknown")

    created = latest.created_at.astimezone(timezone.utc).isoformat() if latest.created_at else "?"
    print("\n--- Latest mention ---")
    print(f"🧵 id: {latest.id}  at (UTC): {created}")
    print(f"👤 from: @{author}")
    print(f"📝 text: {latest.text}")

    reply_text = analyze(latest.text)
    if reply_text:
        r = client.create_tweet(text=reply_text, in_reply_to_tweet_id=latest.id)
        rid = r.data.get("id") if r and r.data else "?"
        print("\n↩  Reply sent")
        print(f"   reply_id: {rid}")
        print(f"   reply_text: {reply_text}")
    else:
        print("✔️ Analysis returned None; skipping reply.")

except tweepy.TooManyRequests as e:
   
    print(" Rate limit hit. Try again after the reset window (or increase the interval / upgrade tier).")
except Exception as e:
    print("[error]", e)








