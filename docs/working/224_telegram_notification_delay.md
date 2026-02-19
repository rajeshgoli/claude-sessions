# sm#224: Telegram Notification Delivery Delay (15-30 min)

**Status:** Investigation complete — spec ready for review
**Issue:** [#224](https://github.com/rajeshgoli/session-manager/issues/224)
**Role:** Scout (root cause analysis, no code changes)
**Related:** sm#184 (PR #195) — stale transcript race (separate bug, partially overlapping fix)

## Problem Statement

Telegram notifications for agent responses are delayed by 15-30 minutes. Messages sent to an agent via `sm send` are delivered on-time, but the response notification back to the sender arrives much later.

**Expected:** Notification arrives within seconds of agent response.
**Observed:** On 2026-02-19, messages sent at 7:57 AM and 8:00 AM were not delivered until 8:12 AM — a 15-minute lag.

## Architecture: Notification Delivery Path

The SM receives Telegram messages via **long polling** (not webhook):

```python
# src/telegram_bot.py:1572
await self.application.updater.start_polling()
```

`start_polling()` is called with **no parameters** — all defaults apply:
- `poll_interval = 0.0` (no sleep between polls)
- `timeout = timedelta(seconds=10)` (Telegram server long-poll hold time)
- `drop_pending_updates = None` (False — messages accumulate across restarts)

Underlying HTTP stack: `python-telegram-bot v22.6` → `httpx` with `HTTPXRequest`:
- `read_timeout = 5.0` (seconds, per-chunk socket read)
- `connect_timeout = 5.0`

For `getUpdates`, python-telegram-bot computes effective read timeout as:
```
effective_read_timeout = read_timeout + timeout.total_seconds()
                       = 5.0 + 10.0 = 15.0 seconds
```

**Critical:** This 15-second timeout is a **per-chunk socket read timeout** (resets on any TCP data), NOT a total request timeout.

## Root Cause Analysis

### Primary: getUpdates Long-Poll Stall (High Confidence — Direct Log Evidence)

**Location:** `src/telegram_bot.py` → `start_polling()` → httpx `getUpdates`

The SM log (`/private/tmp/session-manager.log`) shows a 16-minute gap with zero getUpdates activity:

```
2026-02-19 07:55:53,342 - httpx - INFO - HTTP Request: POST .../getUpdates "HTTP/1.1 200 OK"
[16 minutes of silence — no getUpdates, no log output, no watchdog kill]
2026-02-19 08:12:04,913 - httpx - INFO - HTTP Request: POST .../getUpdates "HTTP/1.1 200 OK"
2026-02-19 08:12:04,923 - src.message_queue - INFO - Queued message 148f803a... for c3bbc6b9
2026-02-19 08:12:04,923 - src.message_queue - INFO - Session c3bbc6b9 already idle, triggering immediate delivery
```

**Mechanism:**

1. SM issues a `getUpdates` long-poll HTTP request to `api.telegram.org`
2. Telegram holds the connection for up to 10s waiting for updates
3. The TCP connection appears to stall silently — the HTTP response is never delivered
4. TCP keepalives keep the connection alive at the network layer, so httpx's 15s per-chunk timeout never fires (a TCP keepalive counts as "data received" for the purpose of resetting the per-chunk read timer)
5. The `getUpdates` coroutine blocks inside `await response.aread()` for 16 minutes
6. No other polling occurs during this time — messages sent to the bot accumulate at Telegram

The library authors acknowledge this limitation explicitly in the `Bot.get_updates()` source:

```python
# Ideally we'd use an aggressive read timeout for the polling. However,
# * Short polling should return within 2 seconds.
# * Long polling poses a different problem: the connection might have been
#   dropped while waiting for the server to return and there's no way of
#   knowing the connection had been dropped in real time.
```

**Why the EventLoopWatchdog did NOT rescue this:**

The watchdog (`main.py`) checks event loop health by posting a `call_soon_threadsafe` callback every 30s and waiting 10s for a response. The asyncio event loop was still responsive — only the `getUpdates` *coroutine* was suspended awaiting network I/O. The watchdog cannot distinguish between "event loop busy processing a normal request" and "one coroutine stuck in a network read." The loop would kill only if the event loop thread itself were frozen (e.g., a blocking call in a coroutine), which did not occur here.

**Why messages batched on delivery:**

`drop_pending_updates` defaults to `False`. During the 16-minute stall, both the 7:57 AM and 8:00 AM messages accumulated at Telegram. When the stall resolved at 8:12:04, all pending updates were returned in a single `getUpdates` response.

### Secondary: Empty Transcript Deferred Notification (Confirmed — Distinct from sm#184)

After message delivery at 8:12:04, the receiving agent (c3bbc6b9) processed the messages and generated responses. The Stop hook fired, but the transcript was empty:

```
2026-02-19 08:12:48 - Stop hook for c3bbc6b9 had empty transcript, deferring notification
2026-02-19 08:13:00 - Stop hook for c3bbc6b9 had empty transcript, deferring notification
2026-02-19 08:28:59 - Notification hook: Stored Claude output + "Sending deferred response notification for c3bbc6b9"
2026-02-19 08:29:01 - sendMessage 200 OK
```

**PR #195 (sm#184 fix) does NOT cover this case.** The 300ms bounded retry in PR #195 applies when `read_transcript()` returns content that matches the previously stored output (stale transcript). When `read_transcript()` returns `None` (empty), the notification is deferred to the next `idle_prompt` Notification hook — which in this case fired 16 minutes later because that is how long the EM agent took to compose its response.

**Classification:** The 16-minute "outbound delay" is the EM's own response time, not a notification bug. However, the EMPTY transcript deferred path is a latent issue: if the next idle_prompt hook is delayed (e.g., agent takes 5 minutes to respond), the notification for the *current* response is delayed by the same amount. This is a variation of sm#184 that requires a separate fix.

### Non-Issue: "17-minute outbound delay"

The issue description mentioned a 17-minute delay for the outbound notification. Investigation shows:
- 8:12:05: Messages delivered to EM (c3bbc6b9)
- 8:12:48 and 8:13:00: Stop hooks fire with empty transcripts → notifications deferred
- 8:28:59: idle_prompt Notification hook fires → deferred notification sent
- 8:29:01: Telegram `sendMessage` 200 OK

The EM was genuinely composing its response from 8:12 to 8:28 — 16 minutes of active processing. The notification fired immediately after the idle_prompt hook. This is correct behavior; the agent had not stopped.

## Proposed Fixes

### Fix 1: Bounded Total Timeout on getUpdates (Recommended)

**Change:** Configure `ApplicationBuilder` with a custom `get_updates_request` that uses `HTTPXRequest` with `read_timeout=30.0` and also apply a `pool_timeout`. The key addition is wrapping the polling loop with `asyncio.wait_for()` at a total timeout that is longer than the Telegram server hold time but short enough to detect stalls.

Python-telegram-bot v20+ exposes `get_updates_request` on `ApplicationBuilder`:

```python
from telegram.request import HTTPXRequest

request = HTTPXRequest(
    read_timeout=30.0,        # Per-chunk timeout
    connect_timeout=5.0,
    pool_timeout=5.0,
)

application = (
    Application.builder()
    .token(self.token)
    .get_updates_request(request)
    .build()
)
```

However, this alone still uses per-chunk timeout. To add a total request timeout, configure httpx directly via `httpx_kwargs`:

```python
request = HTTPXRequest(
    read_timeout=20.0,
    httpx_kwargs={
        "timeout": httpx.Timeout(
            connect=5.0,
            read=20.0,
            write=5.0,
            pool=5.0,
        )
    }
)
```

**Limitation:** This does not fully resolve the TCP-keepalive-defeating-per-chunk-timeout issue. The most robust fix is to use a separate timeout at the Application level:

```python
# In start_polling() call:
await self.application.updater.start_polling(
    poll_interval=0.0,
    timeout=10,
    read_timeout=15,
    write_timeout=5,
    connect_timeout=5,
    pool_timeout=5,
    drop_pending_updates=False,
)
```

These timeout parameters are passed to `_custom_params` in `Updater.start_polling()` and forwarded to `Bot.get_updates()`, which uses them to construct the HTTPXRequest call for that specific request.

**Why:** Explicitly passing timeouts to `start_polling()` overrides the application-level defaults for the polling request specifically, allowing a tighter total timeout to be enforced at the library level.

### Fix 2: Switch to Webhook Mode (Most Robust)

**Change:** Replace `start_polling()` with `start_webhook()` using a public-facing HTTPS endpoint.

**Why:** In webhook mode, Telegram pushes updates to the SM over a persistent HTTPS connection. The SM never has an outbound HTTP request that can stall. Update delivery is guaranteed within Telegram's SLA (~1-2 seconds).

**Tradeoff:** Requires a public HTTPS URL (reverse proxy or ngrok), TLS certificate, and firewall/port exposure. More complex to deploy on a local dev machine. Not appropriate if the SM runs on a laptop with a dynamic IP.

**Recommendation:** Appropriate for production/server deployments, not for local dev.

### Fix 3: Periodic Polling Health Check (Defensive Complement)

**Change:** Add a background coroutine that checks whether `getUpdates` has been called within the last N seconds (e.g., 30s). If not, log a warning and optionally force-restart the updater.

```python
async def _polling_health_monitor(self):
    while True:
        await asyncio.sleep(30)
        elapsed = time.monotonic() - self._last_get_updates_ts
        if elapsed > 45:
            logger.warning(f"getUpdates stalled for {elapsed:.0f}s, restarting updater")
            await self.application.updater.stop()
            await self.application.updater.start_polling()
```

**Why:** Complements Fix 1 by providing a recovery path even when the per-chunk timeout fails to fire. Does not require changing the HTTP configuration.

### Fix 4: Empty Transcript Retry (Sub-issue — Separate Ticket)

The empty transcript deferred notification (secondary finding above) requires the same bounded retry approach as PR #195, but applied when `read_transcript()` returns `None`:

```python
# In Stop hook handler (server.py)
found, last_message = await asyncio.to_thread(read_transcript, transcript_path)
if not found or last_message is None:
    # Transcript not yet written — retry once after 500ms
    await asyncio.sleep(0.5)
    found, last_message = await asyncio.to_thread(read_transcript, transcript_path)
if not found or last_message is None:
    # Still empty — defer to idle_prompt hook (existing behavior)
    app.state.pending_stop_notifications.add(session_manager_id)
```

This reduces the deferred notification path from "defer to next idle_prompt" to "retry 500ms then defer."

## Test Plan

1. **Reproduce stalled polling:**
   - Simulate a stalled TCP connection: `tc qdisc add dev lo root netem delay 60000ms` or use `iptables -A OUTPUT -d api.telegram.org -j DROP` to silently drop the connection
   - Verify that with current code, getUpdates hangs for 15+ minutes
   - Verify that with Fix 1 (explicit start_polling timeouts), the connection is torn down and re-established within ~20s

2. **Validate Fix 1:**
   - Apply `start_polling(timeout=10, read_timeout=15, ...)` configuration
   - Apply the TC/iptables stall simulation
   - Confirm that a new getUpdates call fires within 20s of the stall
   - Confirm no message loss (pending updates returned on next successful poll)

3. **Validate Fix 3 (health monitor):**
   - Apply health monitor with 45s threshold
   - Simulate stall
   - Confirm warning logged and updater restarted within 45s

4. **Validate Fix 4 (empty transcript retry):**
   - Send a message to an agent with a very fast response
   - Confirm that the 500ms retry successfully reads the transcript in most cases
   - Confirm deferred path still works for genuinely slow transcript writes

5. **Edge cases:**
   - Network partition lasting > 60s (connection eventually times out at TCP level)
   - SM restart while pending updates exist at Telegram (verify delivery after restart)
   - Multiple rapid sm send messages during stall window

## Classification

**Epic with 2 sub-tickets:**

1. **sm#225: Fix getUpdates stall — configure explicit timeouts in start_polling()** (Fix 1 + Fix 3)
   - One agent, no context compaction needed
   - Change: pass explicit timeout parameters to `start_polling()`, add polling health monitor coroutine
   - Test: stall simulation + recovery verification

2. **sm#226: Fix empty transcript deferred notification** (Fix 4)
   - One agent, no context compaction needed
   - Change: add 500ms retry in Stop hook handler for `None` transcript
   - Test: fast-response agent notification latency measurement
