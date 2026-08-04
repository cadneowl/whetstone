"""Search HTTP handlers. No `.agents/` here — absence is the normal case."""

from search.repository import SearchRepository


class SearchApi:
    def __init__(self, repo: SearchRepository):
        self._repo = repo

    def get_results(self, request):
        return self._repo.matching(request.args["q"], int(request.args.get("limit", 20)))
