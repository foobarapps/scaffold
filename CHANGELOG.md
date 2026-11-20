# Changelog

What changed in each release, and what to do when upgrading. The section of a version
becomes the notes of its GitHub release when the tag is pushed.

## Unreleased

Security fixes from an audit of 0.9.19, carried onto 0.10.0. Some change behaviour, see
"Upgrading".

### Upgrading from 0.10.0

**Email links.** `EmailNotificationService` builds the links in emails with
`BaseWebApp.build_url`, from the app's `server_name`, and raises without one. Before, a link
rendered during a request (or in a background task started from one) took its host from the
request's Host header, so a client could have a password-reset link point at its own server.
Pass `server_name` to `BaseWebApp`. `url_for` in an email template still takes Quart's
`_external`, `_scheme` and `_anchor`, but no longer `server_name` or `scheme`.

**SMTP.** `SmtpMailSender` with a user name and password now requires STARTTLS unless
`use_tls` is set, and fails rather than log in over a plain connection. Pass
`start_tls=False` for a local relay without TLS, such as a development mail catcher.

**Task queue.** A task row is decoded only into a task type registered on the queue. A row
naming any other type is dead-lettered instead of being imported, so a subclass that
overrides `handle_task()` still has to `register()` every type it handles. `handle_tasks()`
runs at most 10 tasks at a time, and only leases a task when one of them has finished; pass
`concurrency=None` for no limit as before.

**Errors.** An exception that escapes the handling of a request, for example from an error
handler that fails itself, is answered with a plain 500 and logged, outside debug and
testing mode. Before, depending on the Quart version, Quart answered with its debug
traceback page.

### Added

- `PostgresTaskQueue.handle_tasks(concurrency=)`.
- `PostgresPubSubService(reconnect_delay=)`: how long the listener waits before listening
  again after its connection failed.

### Fixed

- A task row could make the worker import any module and call any function in it, with
  arguments from the row.
- `csrf_exempt` on one controller method exempted every method of the same name in the
  module.
- A controller error handler that ran because the controller could not be created made
  Quart answer with its debug traceback page, with file paths, source and frame locals.
- One malformed request (a WebSocket upgrade without a key, or a bad chunked body) stopped
  the dev server for every client.
- A pub/sub notification that was valid JSON but not an object stopped the listener for
  every subscriber, and so did a dropped connection. Subscribing while the last subscriber
  left could strand the new one without a listener or leak a pooled connection.
- `handle_tasks()` leased every due task at once, so a burst made the handlers compete for
  the connection pool and fail on pool timeouts that were charged to their attempts.

## 0.10.0

Fills the gaps an existing app ran into when it moved onto Scaffold: sign-in, translations,
a CLI, a fuller asset pipeline, a Postmark sender, and a task queue with named queues and
leases that are renewed. A few existing behaviours change, see "Upgrading".

### Upgrading from 0.9.x

**Task queue.** The schema goes to version 3.

1. Run `python -m scaffold.task_queue upgrade` (with `--schema` and `--table` if you use
   them) right before you deploy the new code.
2. Deploy the new code without waiting. A worker on 0.9.x insists on its exact schema
   version, so once the upgrade has run it cannot start any more, and the deploy cannot be
   rolled back to 0.9.x. Workers that are already running carry on.
3. Do not let old and new workers run side by side for long. An old worker does not count
   deliveries, so a task it hands out again can still be acknowledged by its earlier
   delivery. Stop the old workers first if that matters to you.

From this release on, `init()` accepts a schema newer than the one it was written for, so
later upgrades can be rolled back.

**Unit of work.** A commit that fails now rolls the session back and closes the unit of
work, and `rollback()` on a closed unit of work does nothing instead of raising
`UnitOfWorkClosedError`. Code that read from the same unit of work after a failed commit
has to create a new one.

**SMTP errors.** `SmtpMailSender` raises `MailDeliveryError`, with the original error as its
cause, instead of letting `aiosmtplib` errors through. If you catch `aiosmtplib` errors,
catch `MailDeliveryError` instead.

**DI container.** Resolving an abstract class that has no registration raises
`MissingDependencyError` instead of a `TypeError` from inside its constructor, and
`*args` / `**kwargs` parameters are no longer resolved as dependencies.

**Assets.** Hashed asset names are served with `Cache-Control: public, max-age=31536000,
immutable`, plain names with `max-age=0, must-revalidate`, and a file that does not exist
answers 404. `get_assets_by_pattern` returns its result sorted.

### Added

- `scaffold.web.login`: session based sign-in with a remember-me cookie, `login_required`
  and `current_user`. The session keys, the cookie and the `REMEMBER_COOKIE_*` settings are
  the ones Flask-Login uses, so an app moving over keeps its users signed in.
- `scaffold.web.i18n`: gettext in templates and in code.
- `scaffold.cli`: a click group whose commands get the app's container (`pass_container`)
  and can be coroutines (`run_async`). Needs the `cli` extra.
- `scaffold.postmark_mail_sender.PostmarkMailSender`. Needs the `postmark` extra.
- `InMemoryMailSender`, `MailDeliveryError` and `MailRecipientInactiveError`.
- `SmtpMailSender`: `use_tls` and `start_tls`. It logs in only when it has both a user name
  and a password.
- `BaseWebApp`: `after_request`, `register_error_handler`, `wrap_asgi_app`, `build_url`,
  `test_request_context`, `add_url_rule`, `assets`, and the options `serve_assets`,
  `refresh_assets` and `static_folder`.
- `@route` can be stacked to serve one method at several rules.
- `CSRF_ENABLED = False` switches the CSRF check off, for tests.
- Assets: `assets.url_for(path)`, `assets.read_manifest(path)`, and a refresh of the static
  folder when its files change (`refresh_assets`, for development).
- `Container`: `override`, `validate`, `add_shutdown_function` and `shutdown`.
- Repositories track entities that cannot be hashed, a second flush updates an entity the
  first one added, and `_after_flush` lets a repository copy back what the database
  generated.
- Task queue:
  - Named queues: `enqueue(..., queue=)` and `consume(queue, stop=, concurrency=, on_progress=)`.
  - `consume` runs a fixed number of consumers, renews the lease of a running task, reports
    progress, finishes the tasks in flight before it stops, and fails if a consumer dies.
  - `max_run_time` bounds how long a handler may run. Past it the handler is cancelled and
    the task counts as failed.
  - `max_attempts` per task, `max_deliveries` per queue, `strict_serialization`, and task
    `metadata` handed to `on_task_start`.
  - A task that cannot be deserialized is dead-lettered.
  - `enqueue` opens the connection pool itself.
  - `GenericFakeTaskQueue.enqueue` takes the same arguments as the real one and records
    them in `enqueued`.

### Fixed

- The dev server delivers the lifespan startup event once. An app that asked for the next
  event before it had finished starting up got a second startup and hung.
- A delivery that has been superseded can no longer acknowledge its task.
