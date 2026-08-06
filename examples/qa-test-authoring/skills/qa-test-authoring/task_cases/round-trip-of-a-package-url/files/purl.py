"""Package URLs: ``pkg:<type>/<namespace>/<name>@<version>``.

The namespace is optional and may itself contain slashes (``github.com/acme``).
"""


class MalformedPurl(ValueError):
    """Raised when a string is not a well-formed package URL."""


def serialize(type_: str, namespace: str, name: str, version: str) -> str:
    """Render the four parts as a package URL. An empty namespace is omitted entirely."""
    if not type_ or not name:
        raise MalformedPurl("a package URL needs at least a type and a name")
    path = f"{namespace}/{name}" if namespace else name
    return f"pkg:{type_}/{path}@{version}"


def parse(text: str) -> tuple[str, str, str, str]:
    """Recover ``(type, namespace, name, version)`` from a package URL."""
    if not text.startswith("pkg:"):
        raise MalformedPurl(f"not a package URL: {text!r}")
    body, _, version = text[4:].rpartition("@")
    if not body:
        raise MalformedPurl(f"package URL has no version: {text!r}")
    type_, _, path = body.partition("/")
    namespace, _, name = path.rpartition("/")
    return type_, namespace, name, version
