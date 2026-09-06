"""The service dependency graph that powers root-signal selection (D3).

PURE MODULE (INV-01). ``ServiceGraph`` is constructed from an adjacency mapping
that somebody else read off disk or fetched from TraceMap -- this module never
opens a file. Keeping the loader outside is what lets the eval harness build a
graph from a fixture without touching a filesystem.

The graph comes from TraceMap when it is available and from a checked-in
``config/service-graph.yaml`` when it is not. **Always ship the YAML fallback**:
a component that only works when another project is deployed is not a product.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ServiceGraph:
    """Directed dependency graph. An edge a -> b means "a depends on b".

    Depth is measured *downward*: a leaf that nothing depends on is shallow, a
    datastore that everything depends on is deep. That orientation is what makes
    ``pick_root_signal`` pick the database rather than the six services shouting
    about it.
    """

    edges: dict[str, tuple[str, ...]] = field(default_factory=dict)

    # -- construction ----------------------------------------------------

    @classmethod
    def from_mapping(cls, mapping: dict[str, list[str]] | None) -> ServiceGraph:
        """Build from a plain ``{service: [depends_on, ...]}`` mapping.

        Services that appear only as a dependency are materialized as nodes with
        no outgoing edges, so ``depth`` and ``distance`` behave for leaves that
        nobody declared explicitly.
        """
        raw = mapping or {}
        edges: dict[str, tuple[str, ...]] = {}
        for service, deps in raw.items():
            edges[str(service)] = tuple(str(d) for d in (deps or []))
        for declared in list(edges.values()):
            for dep in declared:
                edges.setdefault(dep, ())
        return cls(edges=edges)

    @classmethod
    def empty(cls) -> ServiceGraph:
        """No topology available. Correlation degrades to time + labels only.

        This is a real operating mode, not a test artifact: a fresh install has
        no graph, and the correlator must still work -- worse, but work.
        """
        return cls(edges={})

    # -- queries ---------------------------------------------------------

    def services(self) -> frozenset[str]:
        return frozenset(self.edges)

    def dependencies(self, service: str) -> tuple[str, ...]:
        return self.edges.get(service, ())

    def distance(self, source: str | None, target: str | None) -> int | None:
        """Undirected hop count between two services, or None if unrelated.

        Undirected on purpose. A failing database and a failing service that
        depends on it are one hop apart whichever way you walk the edge, and
        correlation cares about *relatedness*, not causality direction --
        causality is ``pick_root_signal``'s job.
        """
        if not source or not target:
            return None
        if source not in self.edges or target not in self.edges:
            return None
        if source == target:
            return 0

        neighbours = self._undirected()
        seen = {source}
        frontier = deque([(source, 0)])
        while frontier:
            node, dist = frontier.popleft()
            for nxt in neighbours.get(node, ()):
                if nxt == target:
                    return dist + 1
                if nxt not in seen:
                    seen.add(nxt)
                    frontier.append((nxt, dist + 1))
        return None

    def depth(self, service: str | None) -> int:
        """How deep in the stack this service sits -- distance from the top.

        A service nothing depends on (an edge API) has depth 0. A datastore that
        the whole graph rests on has the highest depth. That orientation is
        deliberate and it is the one ``pick_root_signal`` needs: it maximizes
        depth to find "the deepest failing dependency", so depth must grow
        *downward through the stack*, not with the length of a service's own
        dependency list.

        Measuring it the other way -- longest chain below -- inverts the answer
        and makes root-signal selection pick the loudest symptom, which is the
        precise failure D3 exists to prevent.

        Cycles are tolerated and truncated rather than raising: a graph derived
        from live traces will contain one eventually, and a correlator that
        crashes on it is worse than one that scores it imperfectly.
        """
        if not service or service not in self.edges:
            return 0
        return self._depth(service, self._dependents(), frozenset())

    def _depth(
        self,
        service: str,
        dependents: dict[str, tuple[str, ...]],
        visiting: frozenset[str],
    ) -> int:
        if service in visiting:
            return 0  # cycle: stop, do not raise
        above = dependents.get(service, ())
        if not above:
            return 0
        nxt = visiting | {service}
        return 1 + max(self._depth(d, dependents, nxt) for d in above)

    def _dependents(self) -> dict[str, tuple[str, ...]]:
        """Reverse adjacency: who depends on each service."""
        rev: dict[str, list[str]] = {node: [] for node in self.edges}
        for node, deps in self.edges.items():
            for dep in deps:
                rev.setdefault(dep, []).append(node)
        return {node: tuple(v) for node, v in rev.items()}

    def _undirected(self) -> dict[str, tuple[str, ...]]:
        out: dict[str, list[str]] = {node: [] for node in self.edges}
        for node, deps in self.edges.items():
            for dep in deps:
                out.setdefault(node, []).append(dep)
                out.setdefault(dep, []).append(node)
        return {node: tuple(dict.fromkeys(neigh)) for node, neigh in out.items()}
