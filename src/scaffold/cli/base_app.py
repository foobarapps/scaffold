import argparse
import asyncio

from .decorators import Command


class BaseCLIApp:
    def __init__(self) -> None:
        self.parser = argparse.ArgumentParser()
        self.subparsers = self.parser.add_subparsers()
        self._register_commands()

    def _register_commands(self) -> None:
        for attr_name in dir(self):
            attr = getattr(self, attr_name)
            if isinstance(attr, Command):
                subparser = self.subparsers.add_parser(
                    attr.command_name,
                    help=attr.command_help,
                )
                for args, kwargs in attr.arguments:
                    subparser.add_argument(*args, **kwargs)
                subparser.set_defaults(func=attr)

    def run(self) -> None:
        args = self.parser.parse_args()

        if hasattr(args, "func"):
            kwargs = dict(vars(args))
            del kwargs["func"]
            result = args.func(self, **kwargs)

            if asyncio.iscoroutine(result):
                asyncio.run(result)

        else:
            self.parser.print_help()
