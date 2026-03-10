# sm#377: durable external job watches

## Scope

Add a first-class Session Manager feature for watching long-running backend jobs and waking an agent only when there is meaningful external progress.

## Problem

Agents currently use ad-hoc shell loops to:

1. sleep
2. inspect a PID and/or output file
3. grep for progress or error markers
4. `sm send` the owning session

That works, but it is the wrong layer. The logic is not durable, is repo-specific, and disappears if the shell loop dies.

## Implemented v1

New CLI:

```bash
sm watch-job add ...
sm watch-job list
sm watch-job cancel <watch-id>
```

New server/API:

1. `POST /job-watches`
2. `GET /job-watches`
3. `DELETE /job-watches/{id}`

New persistence:

1. SQLite table `job_watch_registrations`
2. startup recovery alongside reminders and parent-wake

## Watch model

Each watch targets one SM session and polls on an interval using declarative inputs:

1. optional PID
2. optional output/log file
3. optional progress regex
4. optional done regex
5. optional error regex
6. optional exit-code file
7. notify-on-change vs notify-every-poll

Notifications are queued back to the target session as SM messages:

1. progress
2. completion
3. error
4. generic process exit when no better terminal signal exists

## Important constraint

SM cannot recover the true exit code of an arbitrary pre-existing PID after it exits. For that reason, v1 supports exit-code-based completion via an explicit `exit_code_file`.

Expected usage:

```bash
( your_job > job.log 2>&1; printf '%s\n' $? > job.exit ) &
pid=$!
sm watch-job add --pid "$pid" --file job.log --exit-code-file job.exit ...
```

## Acceptance mapping

1. durable job watch registration
2. startup recovery
3. PID/file/regex polling
4. exit-code-file completion/error
5. list/cancel visibility from CLI

## Classification

Single ticket.
