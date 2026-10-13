"""Entry point: `uv run python -m backend.mcp.servers.github_mcp` (stdio)."""

from backend.observability.logging import configure_logging


def main() -> None:
    configure_logging()
    from backend.mcp.servers.github_mcp.server import mcp

    mcp.run()  # stdio transport; stdout is protocol-reserved


if __name__ == "__main__":
    main()
