"""Command line entry point. Commands are registered as the features land."""

import typer

app = typer.Typer(no_args_is_help=True, help="Freight load agent")


@app.callback()
def main() -> None:
    """Freight load agent."""
