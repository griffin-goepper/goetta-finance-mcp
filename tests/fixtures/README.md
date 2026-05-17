# Test fixtures

`simplefin_demo_response.json` is a synthetic SimpleFIN `/accounts` response
that mirrors the shape returned by the public SimpleFIN Bridge demo. It
contains two accounts (one checking, one brokerage), one pending transaction
to exercise the drop-pending path, and a mix of fields present/absent.

To capture a fresh response from the live demo:

1. `curl https://beta-bridge.simplefin.org/simplefin/create` → returns a
   base64-encoded setup token (one-shot per call).
2. base64-decode the token to a claim URL, `POST` it with no body. The
   response body is the access URL (contains basic-auth user/pass).
3. `curl 'https://USER:PASS@beta-bridge.simplefin.org/simplefin/accounts?start-date=...&end-date=...'`
   → response JSON.

Keep this fixture small and deterministic. Tests must never hit the live
Bridge.
