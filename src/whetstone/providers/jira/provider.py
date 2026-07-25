from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any

from whetstone.domain.issue import Issue, IssueRef
from whetstone.providers.base import Capability
from whetstone.providers.jira.client import DEFAULT_SEARCH_PATH, JiraHttp
from whetstone.providers.jira.normalize import (
    DEFAULT_DEFECT_TYPES,
    issue,
    issue_key,
    issue_ref,
    remote_link_urls,
)

# Only what the corpus builder reads. Asking for everything makes Jira assemble rendered fields and
# changelogs nobody looks at, on every page of a multi-year backfill.
FIELDS = "summary,description,issuetype,priority,labels,components,resolution,resolutiondate"

# Project keys go straight into a JQL string. Jira keys are uppercase alphanumerics and underscores,
# so anything else is either a typo or an attempt to smuggle in a clause.
_PROJECT_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


class JiraConnector:
    """Jira implementation of `IssueConnector` (REST v3 on Cloud, v2 on Server/Data Center)."""

    def __init__(
        self,
        http: JiraHttp,
        *,
        base_url: str = "",
        search_path: str = DEFAULT_SEARCH_PATH,
        defect_types: tuple[str, ...] = DEFAULT_DEFECT_TYPES,
        jql_filter: str = "",
    ) -> None:
        self._http = http
        self._base_url = base_url
        self._search_path = search_path
        self._defect_types = defect_types
        self._jql_filter = jql_filter

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> JiraConnector:
        base_url = str(config["base_url"])
        token_env = str(config.get("token_env", "JIRA_TOKEN"))
        types = config.get("defect_types")
        return cls(
            JiraHttp(
                base_url,
                os.environ.get(token_env, ""),
                email=str(config.get("email", "")),
            ),
            base_url=base_url,
            search_path=str(config.get("search_path", DEFAULT_SEARCH_PATH)),
            defect_types=tuple(str(t).lower() for t in types) if types else DEFAULT_DEFECT_TYPES,
            jql_filter=str(config.get("jql_filter", "")),
        )

    def capabilities(self) -> set[Capability]:
        return {Capability.tracker}

    def jql(self, project: str, since: datetime) -> str:
        """Resolved issues in a project since a date, newest last.

        `resolution IS NOT EMPTY` rather than `resolution = Done`: every workflow names its
        done-state differently, and the question here is only whether the issue was closed out.
        """
        if not _PROJECT_KEY_RE.match(project):
            raise ValueError(
                f"invalid Jira project key {project!r}: expected letters, digits and underscores"
            )
        clauses = [
            f'project = "{project}"',
            f'resolutiondate >= "{since.strftime("%Y-%m-%d")}"',
            "resolution IS NOT EMPTY",
        ]
        if self._jql_filter:
            clauses.append(f"({self._jql_filter})")
        return " AND ".join(clauses) + " ORDER BY resolutiondate ASC"

    # --- IssueConnector ------------------------------------------------------
    def list_resolved_issues(self, project: str, since: datetime) -> list[IssueRef]:
        return [
            issue_ref(issue_key(payload.get("key", "")), base_url=self._base_url)
            for payload in self._http.search(self._search_path, self.jql(project, since), FIELDS)
            if payload.get("key")
        ]

    @property
    def _api(self) -> str:
        """The REST root, derived from `search_path`.

        Cloud is `/rest/api/3/search/jql`, Server and Data Center `/rest/api/2/search` — different
        versions *and* different suffixes. Deriving the root from the one setting an operator
        already has to get right beats a second knob that can disagree with it.
        """
        head, separator, _ = self._search_path.partition("/search")
        return head if separator else DEFAULT_SEARCH_PATH.partition("/search")[0]

    def get_issue(self, ref: IssueRef) -> Issue:
        payload = self._http.get_json(f"{self._api}/issue/{ref.key}", {"fields": FIELDS})
        return issue(
            payload,
            base_url=self._base_url,
            defect_types=self._defect_types,
            linked_urls=self._remote_links(ref.key),
        )

    def _remote_links(self, key: str) -> list[str]:
        """Remote links are optional and often unconfigured, so a failure here is not a failure.

        The corpus builder's primary link is the issue key mentioned in a merge request, which needs
        no Jira call at all; this only ever adds. Letting a 404 on an instance without the
        integration abort a whole backfill would trade a nice-to-have for the entire run.
        """
        resp = self._http.request("GET", f"{self._api}/issue/{key}/remotelink")
        if resp.status_code != 200:
            return []
        try:
            return remote_link_urls(resp.json())
        except ValueError:
            return []
