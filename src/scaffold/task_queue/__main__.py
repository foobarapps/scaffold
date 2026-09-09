"""Entry point for `python -m scaffold.task_queue`, the schema management CLI."""

from scaffold.task_queue.migrations import main

if __name__ == "__main__":
    main()
