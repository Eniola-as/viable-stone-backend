from django.conf import settings
from django.core.files.storage import FileSystemStorage


class PrivateMediaStorage(FileSystemStorage):
    """Filesystem storage rooted outside the public static tree.

    Files are only reachable through an authenticated, branch-scoped download
    view, never via a directly guessable URL.
    """

    def __init__(self, **kwargs):
        kwargs.setdefault("location", str(settings.PRIVATE_MEDIA_ROOT))
        kwargs.setdefault("base_url", settings.MEDIA_URL)
        super().__init__(**kwargs)
