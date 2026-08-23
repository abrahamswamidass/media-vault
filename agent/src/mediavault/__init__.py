"""
Media Vault agent — the only thing in this project that touches your files.

Layers, outermost first:

    cli.py        typed by you
    sync/         driven by intents from the web module
    actions/      every mutation, as a Command object (dry-run by default)
    connectors/   one adapter per source: NAS, Drive, archive, Amazon
    ports.py      the Connector interface all adapters implement
"""
__version__ = "0.2.0"
