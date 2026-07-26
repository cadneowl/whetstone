from __future__ import annotations

import os
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

from whetstone.domain.change import CodeChange
from whetstone.domain.refs import RepoRef
from whetstone.domain.review import FileBlob, MergeRequestRef, ReviewedChange, ReviewThread
from whetstone.providers.base import Capability, ConnectorError
from whetstone.providers.gitlab.client import GitLabHttp
from whetstone.providers.gitlab.normalize import file_change, mr_ref, review_thread


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
        params = {"state": "merged", "updated_after": since.isoformat(), "order_by": "updated_at"}
        return [mr_ref(repo, m) for m in self._http.paginate(endpoint, params)]

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
