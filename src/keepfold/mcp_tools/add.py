"""keepfold_add: a new page (concept, problem, note, resource, decision, ...) and its links.
A mixin of `keepfold.mcp_server.Keepfold`."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Literal

from keepfold import access, dates, loops, people
from keepfold.catalog import KINDS as CATALOG_KINDS
from keepfold.catalog import inbox_add, page_slug
from keepfold.events import check_assessment_result, local_day
from keepfold.index import _edge_targets, _target, _wrap
from keepfold.mcp_server import (
    ADD_FIELDS,
    DECISION_BODY,
    LINK_FIELDS,
    MORE_NEXT,
    PIN_KINDS,
    WIKI_DEFAULTS,
    _pin_day,
    _same_company,
    _unprivate,
)
from keepfold.prime import PLAYBOOK_TAG
from keepfold.vault import ENUMS, VaultError, invalid, is_private, scan_secrets

if TYPE_CHECKING:  # build_server puts the SDK's names in this module's globals
    from mcp.server.mcpserver import Context


class AddTools:
    """keepfold_add and the pages it writes."""

    def keepfold_add(self, kind: Literal["domain", "area", "concept", "problem", "assessment",
                                       "application", "mistake", "resource", "note", "project",
                                       "decision", "loop", "person"],
                   title: str, parent: str | None = None, areas: list[str] | None = None,
                   fields: dict[str, Any] | None = None, raw_text: str | None = None,
                   force: bool = False, key: str | None = None,
                   ctx: Context | None = None) -> dict[str, Any]:
        # L1: a <private> span is never written; relative dates get their day
        title, raw_text = _unprivate(title), _unprivate(raw_text)
        fields = {k: _unprivate(v) for k, v in (fields or {}).items()}
        if kind in PIN_KINDS and (today := _pin_day(self)):
            title = dates.pin(title, today)
            if isinstance(fields.get("summary"), str):
                fields["summary"] = dates.pin(fields["summary"], today)
        if areas and kind != "concept":
            raise invalid("Only a concept takes areas.")
        if raw_text is not None and kind != "resource":
            raise invalid("raw_text is only for kind resource.")
        if kind == "note" and fields.get("note_type") == "fleeting":
            # a fleeting note is one inbox.md line, not a file (phase4-wiki.md §2a)
            nid = inbox_add(self.vault, title, "fleeting")
            hint = self.sessions.note("added", change={
                "tool": "add", "id": nid, "kind": kind, "op": "create", "target": None,
                "note_type": "fleeting"})
            return {"id": nid, "path": "inbox.md", "next": self._next(MORE_NEXT) + hint}
        if kind == "person":  # V4: private unless fields.private is false; id people/<slug>
            pid = people.add(self.vault, title, fields)
            hint = self.sessions.note("added", change={
                "tool": "add", "id": pid, "kind": kind, "op": "create", "target": None})
            return {"id": pid, "path": f"{pid}.md", "next": "Fill '## Who they are to me' and "
                    f"'## How I write to them' with keepfold_patch(id='{pid}') from what the "
                    "user tells you." + hint}
        if kind in CATALOG_KINDS:
            if self.session.mode == "review":
                raise VaultError("read_only_mode", f"Review mode cannot add a {kind}; note it "
                                 "and add it in a learn session.")
            extra = set(fields) - {"aliases", "requires"}
            if extra:
                raise invalid(f"A {kind} takes only aliases and requires in fields.")
            nid = self.catalog.add(kind, title, parent, fields.get("aliases") or [],
                                   fields.get("requires") or [], force, areas or [])
        else:  # G-Q9-15: a page added after the chat read outside text says so (`exposed`)
            nid = self._add_note(kind, title, parent, fields, force, raw_text,
                                 exposed=self.session.exposed)
        adopted = kind in CATALOG_KINDS and self.catalog.adopted
        hint = self.sessions.note("added", nid if kind == "concept" else None, change={
            "tool": "add", "id": nid, "kind": kind, "op": "adopt" if adopted else "create",
            "target": None, **({"note_type": fields.get("note_type")} if kind == "note" else {})})
        path = self.vault.path_for(kind, nid).relative_to(self.vault.root).as_posix()
        out = {"id": nid, "path": path, "next": self._next(MORE_NEXT) + hint}
        if kind in ("resource", "note"):
            auto = self._auto_topic(kind, nid, fields)
            out["next"] = auto.pop("next", "") + out["next"]
            out.update(auto)
        if kind == "note" and fields.get("note_type") == "permanent":
            out["next"] = ("Append each link with a short reason under ## Links "
                           "(keepfold_patch append_to_section). ") + out["next"]
        if adopted:
            out["adopted"] = True  # the hand-made note was registered in the catalog as is
        return out

    def _auto_topic(self, kind: str, nid: str, fields: dict) -> dict:
        """REVIEW-1005 §5.4: file a new public resource or note under its nearest topic hub
        when confident, else suggest the top 3. Nothing without the embedder, when
        fields.related already names a hub, or for a hub; it never fails the add."""
        from keepfold import semantic, topics

        tags = fields.get("tags")
        embed = semantic.load_embedder()  # never downloads
        if embed is None or (isinstance(tags, list) and "topic" in tags):
            return {}
        try:
            self.index.refresh()
            tree = topics.tree(self.index)
            n = self.index._notes.get(nid)  # absent when private, or under a private area
            if n is None or topics.page_hubs({"fields": fields}, tree.parent):
                return {}
            a = topics.attach_text(self.index, embed, semantic._text(n))
            if a.hub:
                topics.place(self.vault, nid, [a.hub], kind)
        except Exception as e:  # the page is written; a broken model or hub must not undo that
            logging.getLogger(__name__).warning("topic attach skipped: %s", e)
            return {}
        if a.hub:
            self.sessions.note("patched", change={"tool": "patch", "id": nid, "kind": kind,
                                                  "op": "set_topics", "target": a.hub})
            return {"topic": {"id": a.hub, "title": _wrap(a.hub, tree.title[a.hub])},
                    "next": f"Filed under [[{a.hub}]]; if that is wrong, fix it with "
                            "keepfold_patch op=set_topics. "}
        if not a.top:
            return {}
        return {"topic_suggestions": [{"id": h, "title": _wrap(h, tree.title[h]),
                                       "score": round(s, 3)} for h, s in a.top],
                "next": "No topic was a clear fit. If one of topic_suggestions clearly fits, "
                        f"file it with keepfold_patch(id='{nid}', op='set_topics', target='', "
                        "text='<hub id>'); else leave the page unfiled. "}

    def _add_note(self, kind: str, title: str, parent: str | None, fields: dict,
                  force: bool, raw_text: str | None = None, exposed: bool = False) -> str:
        extra = set(fields) - ADD_FIELDS[kind]
        if extra:
            raise invalid(f"Fields not allowed on a {kind}: {', '.join(sorted(extra))}; allowed: "
                       f"{', '.join(sorted(ADD_FIELDS[kind]))}.")
        for name, value in fields.items():
            allowed = ENUMS.get((kind, name))
            if allowed and (not isinstance(value, str) or value not in allowed):
                raise invalid(f"{name} must be one of {', '.join(sorted(allowed))}.")
        if kind == "note" and "scope" in fields and fields["scope"] not in access.SCOPES:
            raise invalid(f"scope must be one of {', '.join(access.SCOPES)}.")  # G4 playbooks
        if kind == "assessment":
            check_assessment_result(fields.get("score"), fields.get("date"))
        if kind in WIKI_DEFAULTS:
            fields = self._wiki_fields(kind, fields, raw_text)
        if kind == "problem" and fields.get("concepts") is not None:  # B9: unknown -> did_you_mean
            v = fields["concepts"]
            fields = {**fields, "concepts": [f"[[{self._concept(_target(n) or str(n))}]]"
                                             for n in (v if isinstance(v, list) else [v])]}
        slug = page_slug(title)
        if not slug:
            raise invalid("The title needs at least one letter or digit.")
        if kind == "mistake" and parent:
            cid = self._concept(parent)
            domain = self.catalog.entries[cid].domain
            slug = f"{domain}.{slug}" if domain else slug
            fields.setdefault("concepts", [f"[[{cid}]]"])
        base = slug[:81].rstrip("-.")
        self.index.refresh()
        dups = self.index.title_matches(title, kind, 3)
        exists = self.vault.path_for(kind, base).is_file()
        if exists and base not in dups:
            dups.insert(0, base)
        if dups and not force:
            e = VaultError("duplicate_suggested", f"'{title}' looks like {', '.join(dups)}; "
                           "use the existing one or pass force=true.")
            e.did_you_mean = dups
            raise e
        raw = self.vault.root / "raw"
        nid, n = base, 2
        while self.vault.path_for(kind, nid).is_file() or \
                (raw_text is not None and (raw / f"{nid}.md").exists()):
            nid = f"{base[:78].rstrip('-.')}-{n}"
            n += 1
        if kind == "assessment":  # after the duplicate check: these may create concepts
            fields = self._assessment_links(title, fields)
        elif kind == "application":
            fields = self._application_links(title, fields)
        body = f"# {title}\n\n## Notes\n"
        if kind == "decision":
            body = f"# {title}{DECISION_BODY}"
        elif fields.get("note_type") == "permanent" and kind == "note":
            body += "\n## Links\n"
        elif kind == "note" and PLAYBOOK_TAG in (fields.get("tags") or ()):
            body = f"# {title}\n\n## Steps\n\n## Pitfalls\n\n## Done when\n"  # G4, filled by patch
        if kind in WIKI_DEFAULTS:
            fields = {"kind": kind, **fields}
        if exposed:  # server-stamped: never in ADD_FIELDS, so never from tool input
            fields = {**fields, "exposed": True}
        if raw_text is None:
            self.vault.create_note(kind, nid, {"title": title, **fields}, body)
            return nid
        with self.vault.lock():  # raw/ files are written once and never overwritten
            if (raw / f"{nid}.md").exists():
                raise invalid(f"raw/{nid}.md already exists; retry the add.")
            self.vault.create_note(kind, nid, {"title": title, **fields,
                                               "source": f"raw/{nid}.md"}, body)
            self.vault.write_text(raw / f"{nid}.md", raw_text)
        return nid

    def _assessment_links(self, title: str, fields: dict) -> dict:
        """fields.concepts (names) and the names in fields.tests become `tests` weights on
        `<kind>:<id>` (a known domain, area or concept, else a concept made by _link_names; a
        refused name goes to inbox.md), so the planner and the graph see them. With no
        `application`, the public application of the same company is linked."""
        fields, hide = dict(fields), self._hidden()
        cs, tests = fields.pop("concepts", None), fields.get("tests")
        names = [cs] if isinstance(cs, str) else cs if isinstance(cs, list) else []
        if isinstance(tests, list):
            names, tests = [*tests, *names], {}
        out, todo = {}, []
        for k, w in (tests.items() if isinstance(tests, dict) else ()):
            try:
                i = self.catalog.resolve(_target(k) or str(k), CATALOG_KINDS, skip=hide)
                out[f"{self.catalog.entries[i].kind}:{i}"] = w
            except VaultError:
                if ":" in str(k):
                    out[k] = w  # an id key Keepfold does not know (yet): kept as written
                else:
                    todo.append((str(k), w))
        todo += [(str(n), 1) for n in names]
        for (name, w), (_, cid, _) in zip(todo, self._link_names([n for n, _ in todo], hide),
                                          strict=True):
            if cid:
                out.setdefault(f"concept:{cid}", w)
            else:
                inbox_add(self.vault, name, "tested by an assessment")
        if out or tests is not None:
            fields["tests"] = out
        if not fields.get("application"):
            me = {**fields, "title": title}
            apps = [n.id for n in self.vault.iter_notes("application")
                    if not is_private(n) and _same_company(me, n.fields)]
            if apps:
                fields["application"] = f"[[{apps[0]}]]"
        return fields

    def _application_links(self, title: str, fields: dict) -> dict:
        """fields.assessment(s) as `[[id]]` links when the page exists (by id or title slug),
        plus every public assessment of the same company."""
        fields = dict(fields)
        one, v = fields.pop("assessment", None), fields.get("assessments")
        names = [*([v] if isinstance(v, str) else v if isinstance(v, list) else []),
                 *([one] if one else [])]
        found = {n.id: n for n in self.vault.iter_notes("assessment") if not is_private(n)}
        me = {**fields, "title": title}
        links = [f"[[{t}]]" if (t := _target(n) or page_slug(str(n))) in found else str(n)
                 for n in names]
        links += [f"[[{i}]]" for i, n in found.items() if _same_company(me, n.fields)]
        if links:
            fields["assessments"] = list(dict.fromkeys(links))
        return fields

    def _wiki_fields(self, kind: str, fields: dict, raw_text: str | None) -> dict:
        """Defaults, [[link]] form and the §2a rules for a new resource/note/project/decision."""
        fields = {**WIKI_DEFAULTS[kind], **fields}
        if kind in ("decision", "loop"):
            fields.setdefault("date", local_day(self._now()))
        for name in ("date", "due", "review_on"):
            if fields.get(name) is not None and not loops.parse_day(fields[name]):
                raise invalid(f"{name} must be a date like 2026-11-30.")
        if kind == "decision":  # SPEC §0 U7: follow up 21 days after the decision
            opts, chosen = fields.get("options"), fields.get("chosen")
            if opts is not None and not (isinstance(opts, list) and 2 <= len(opts) <= 6 and all(
                    isinstance(o, str) and 0 < len(o.strip()) <= 80 for o in opts)):
                raise invalid("options must be a list of 2 to 6 strings of 80 characters or fewer.")
            if chosen is not None and (opts is None or chosen not in opts):
                raise invalid("chosen must be one of options (and needs options).")
            fields.setdefault("review_on", (loops.parse_day(fields["date"]) + timedelta(
                days=loops.REVIEW_DAYS)).isoformat())
        for name in (n for n in LINK_FIELDS if n in fields):  # WIKI_DEFAULTS set each kind's own
            v = fields.get(name)
            items = v if isinstance(v, list) else [v] if v else []
            fields[name] = [s if s.startswith("[[") else f"[[{s}]]" for s in map(str, items)]
        if fields.get("project"):
            t = _edge_targets(fields["project"])
            fields["project"] = f"[[{t[0]}]]" if t else str(fields["project"])
        if fields.get("source") is not None:
            if raw_text is not None:
                raise invalid("Pass raw_text or fields.source, not both.")
            self._raw_path(str(fields["source"]))
        if raw_text is not None:
            scan_secrets(raw_text)  # before the note exists, so a refusal leaves nothing behind
        if fields.get("note_type") == "permanent":
            linked = any(self.vault.path_for(k, t).is_file() for t in
                         _edge_targets(fields["related"]) for k in self.vault.KIND_DIRS)
            r = self.vault.root
            empty = not any((r / "notes").glob("*.md")) and not any((r / "concepts").glob("*.md"))
            if not linked and not empty:
                raise invalid("A permanent note must link to at least one existing page: pass "
                           "fields.related with page ids, then give each link a short reason "
                           "in ## Links.")
        return fields
