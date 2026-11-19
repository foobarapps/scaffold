# Changelog

What changed in each release, and what to do when upgrading. The section of a version
becomes the notes of its GitHub release when the tag is pushed.

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
  - `max_run_time` bounds how long a lease is renewed.
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
