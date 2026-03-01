# sm#326: codex-app retirement and provider mapping enforcement

## Scope

Implement codex-app cutover enforcement semantics:

1. Enforce provider mapping phase for API creation paths.
2. Retire existing codex-app sessions deterministically at post-cutover.
3. Clean request ledger + queued message artifacts with explicit terminal reason.
4. Reject post-cutover mutating actions against retired codex-app sessions.

## Implementation summary

### Provider mapping enforcement

API creation paths now enforce codex-app policy:

1. `POST /sessions`
2. `POST /sessions/create`
3. `POST /sessions/spawn`

When `codex_provider_policy.allow_create=false`, these return policy rejection text.

### Existing-session retirement

`SessionManager` adds deterministic codex-app retirement:

1. `retire_codex_app_sessions(reason=provider_retired_codex_app)`
2. `_retire_codex_app_session_state(...)` internal cleanup helper

Retirement effects:

1. codex request ledger orphaned with `error_code=provider_retired_codex_app`
2. message queue pending artifacts removed via `retire_session_queue(...)`
3. session state forced to `STOPPED` with terminal completion/error reason
4. `codex_app_retired` event appended for audit visibility

### Restore/bootstrap behavior

When `provider_mapping_phase=post_cutover`, restored codex-app sessions are immediately marked retired during state load (no auto-resume semantics).

### Post-cutover action rejection

Mutating codex-app actions now return `410` with explicit retirement reason:

1. `POST /sessions/{id}/input`
2. `POST /sessions/{id}/codex-requests/{request_id}/respond`
3. `POST /sessions/{id}/clear`

## Acceptance mapping

1. Existing codex-app sessions retire deterministically with explicit reason.
2. Pending ledger + queue artifacts are cleaned with provider-retired semantics.
3. Provider mapping behavior is phase-enforced across create/spawn API paths.
