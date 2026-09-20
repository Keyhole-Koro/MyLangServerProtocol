"""Run the MyLang language server over stdio."""

from .protocol.server import LspServer


if __name__ == "__main__":
    LspServer().run()
