"""Send runtime and GUI messages to the shared Cloudflare log store."""


class SharedLogger:
    """Callable logger used by both the desktop app and hosted worker."""

    def __init__(self, store, source, on_message=None, echo=False):
        self.store = store
        self.source = str(source)
        self.on_message = on_message
        self.echo = bool(echo)

    def __call__(self, message):
        text = str(message)
        if self.echo:
            print(text, flush=True)
        if self.on_message is not None:
            self.on_message(text)
        try:
            self.store.add_log(self.source, text)
        except Exception as exc:
            if self.echo:
                print(f"Log upload failed: {exc}", flush=True)
