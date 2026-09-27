"""Connected components of the judge-project graph (union-find), as in the lab's `lab/graph.py`.

Two projects can only be compared if some chain of shared judges links them. A method that
corrects for judge lean has nothing to calibrate one group's judges against another's, so each
component is fitted and ranked on its own; an overall rank exists only when there is one.
"""

from __future__ import annotations


def components(projects, reviews):
    """[(project ids, reviews)] per component, largest first (ties: earliest project in input
    order). `projects`: the reviewed projects in input order. Review order is preserved."""
    parent = {}

    def find(x):
        root = x
        while parent.setdefault(root, root) != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    for project in projects:
        find(("p", project))
    for review in reviews:
        a, b = find(("p", review.project_id)), find(("j", review.judge_id))
        if a != b:
            parent[a] = b
    groups = {}
    for project in projects:
        groups.setdefault(find(("p", project)), []).append(project)
    ordered = sorted(groups.values(), key=lambda ps: -len(ps))  # stable: input order breaks ties
    out = []
    for members in ordered:
        member_set = set(members)
        out.append((tuple(members), tuple(r for r in reviews if r.project_id in member_set)))
    return out
