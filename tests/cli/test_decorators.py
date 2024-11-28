from scaffold.cli.decorators import Command, command


def test_command_decorator() -> None:
    @command()
    def my_command() -> None:
        pass

    assert isinstance(my_command, Command)
    assert my_command.name == "my_command"
