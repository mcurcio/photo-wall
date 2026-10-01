# Reboot receipt carrier proposal — NOT applied

This is a review artifact only. Automatic approval review rejected applying this authentication-boundary change; explicit user approval remains pending. Do not apply it merely because it appears in the handoff. Original command identity, producer, scope and expiry must remain authoritative; renewed transport must grant no effect authority.

```diff
--- a/central/fleet/node_ingest.py
+++ b/central/fleet/node_ingest.py
@@ -105,8 +105,10 @@
 
     def _response_in(self, conn, principal, message: NodeCommandResponseV2,
                      canonical: bytes) -> dict:
-        if (message.command_session_id != principal.grant.session_id
-                or message.scope != principal.grant.scope):
+        # A renewed credential may carry the original immutable receipt. The
+        # original command session remains validated against stored command bytes;
+        # carrier renewal never renews command expiry or authorizes an effect.
+        if message.scope != principal.grant.scope:
             raise NodeControlError("node_response_scope_mismatch", 403)
         row = conn.execute("SELECT payload FROM node_reboot_commands WHERE command_id=%s",
                            (message.command_id,)).fetchone()
```
