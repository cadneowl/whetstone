from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from whetstone.domain.change import CodeChange
from whetstone.domain.refs import RepoRef
from whetstone.domain.review import FileBlob, MergeRequestRef, ReviewedChange, ReviewThread
from whetstone.providers.base import Capability, ConnectorError
from whetstone.providers.gitlab.client import GitLabHttp
from whetstone.providers.gitlab.normalize import file_change, mr_ref, review_thread

# A GitLab merge-request URL: `<base>/<group>/<project>/-/merge_requests/<iid>`, where the project
# path may itself be nested groups (`a/b/c`). Anything after the number — `/diffs`, `#note_5`, a
# query string — is ignored, so a link copied from anywhere in the MR still resolves.
_MR_PATH = re.compile(r"^/(?P<project>.+?)/-/merge_requests/(?P<iid>\d+)(?:[/?#].*)?$")


def parse_merge_request_url(url: str) -> tuple[str, str, int]:
    """`(base_url, project_path, iid)` from a GitLab merge-request URL.

    Splits the human URL a person copies from their browser into the three things the API needs. The
    caller still decides whether the host is one it will send a token to — this only parses.

    Raises `ValueError`, with the offending URL in the message, when it is not a merge-request URL.
    """
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"{url!r} is not an http(s) URL")
    match = _MR_PATH.match(parsed.path)
    if not match:
        raise ValueError(
            f"{url!r} is not a GitLab merge-request URL "
            "(expected …/<project>/-/merge_requests/<number>)"
        )
    return f"{parsed.scheme}://{parsed.netloc}", match.group("project"), int(match.group("iid"))


class GitLabConnector:
    """GitLab implementation of SourceConnector + ReviewConnector (GitLab API v4)."""

    def __init__(self, http: GitLabHttp) -> None:
        self._http = http

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> GitLabConnector:
        base_url = config["base_url"]
        token_env = config.get("token_env", "GITLAB_TOKEN")
        token = os.environ.get(token_env, "")
        return cls(GitLabHttp(base_url, token))

    def capabilities(self) -> set[Capability]:
        return {Capability.source, Capability.review}

    @staticmethod
    def _pid(repo: RepoRef) -> str:
        return quote(repo.path, safe="")

    # --- SourceConnector -----------------------------------------------------
    def get_file(self, repo: RepoRef, ref: str, path: str) -> FileBlob | None:
        endpoint = f"/api/v4/projects/{self._pid(repo)}/repository/files/{quote(path, safe='')}/raw"
        resp = self._http.request("GET", endpoint, params={"ref": ref})
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return FileBlob(path=path, ref=ref, content=resp.text)

    def get_change(self, repo: RepoRef, base: str, head: str) -> CodeChange:
        endpoint = f"/api/v4/projects/{self._pid(repo)}/repository/compare"
        data = self._http.get_json(endpoint, params={"from": base, "to": head})
        files = [file_change(d) for d in data.get("diffs", [])]
        return CodeChange(repo=repo, base_ref=base, head_ref=head, files=files)

    # --- ReviewConnector -----------------------------------------------------
    def list_reviewed_changes(self, repo: RepoRef, since: datetime) -> list[MergeRequestRef]:
        endpoint = f"/api/v4/projects/{self._pid(repo)}/merge_requests"
        # `sort` is stated rather than left to GitLab's default. A corpus walk writes as it goes and
        # may be stopped at any point — by an operator, a timeout, a token expiring — so the order
        # decides which history survives a partial run. Newest first means what you keep is the
        # review activity most likely to still describe how the team works.
        params = {
            "state": "merged",
            "updated_after": since.isoformat(),
            "order_by": "updated_at",
            "sort": "desc",
        }
        return [mr_ref(repo, m) for m in self._http.paginate(endpoint, params)]

    def get_merge_request(self, repo: RepoRef, iid: int) -> MergeRequestRef:
        """One merge request by iid — **open or merged**.

        `list_reviewed_changes` filters to merged, because mining history has no use for a branch
        still being argued about. Reviewing one live is the opposite case: the whole point is that
        it has not landed yet.
        """
        endpoint = f"/api/v4/projects/{self._pid(repo)}/merge_requests/{iid}"
        try:
            return mr_ref(repo, self._http.get_json(endpoint))
        except httpx.HTTPError as exc:
            raise ConnectorError(f"{repo.path}!{iid}: {exc}") from exc

    def get_review(self, mr: MergeRequestRef) -> ReviewedChange:
        try:
            return self._fetch_review(mr)
        except httpx.HTTPError as exc:
            # Translated at the adapter boundary so a corpus walk can decide whether one unreachable
            # merge request is worth abandoning the other thousand — without importing `httpx` to
            # ask, and without a blanket `except Exception` that would swallow our own bugs too.
            raise ConnectorError(f"{mr.repo.path}!{mr.iid}: {exc}") from exc

    def _fetch_review(self, mr: MergeRequestRef) -> ReviewedChange:
        base = f"/api/v4/projects/{self._pid(mr.repo)}/merge_requests/{mr.iid}"
        detail = self._http.get_json(base)
        ref = mr_ref(mr.repo, detail)

        files = [file_change(d) for d in self._http.paginate(f"{base}/diffs")]
        change = CodeChange(
            repo=mr.repo, base_ref=ref.base_sha, head_ref=ref.head_sha, files=files
        )

        threads: list[ReviewThread] = []
        for disc in self._http.paginate(f"{base}/discussions"):
            thread = review_thread(disc)
            if thread is not None:
                threads.append(thread)

        return ReviewedChange(mr=ref, change=change, threads=threads)
