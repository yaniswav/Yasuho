"""Structural guard: no slash command may make Discord wait past its 3s window.

WHY THIS GUARD EXISTS
---------------------
A slash (or hybrid, invoked as slash) command must send its FIRST interaction
response within three seconds of the invocation, or acknowledge it with a defer.
Miss that and the token is gone: every later send raises
``404 Not Found (error code: 10062): Unknown interaction``, the user is left on
"The application did not respond", and whatever the command did in the meantime
still happened - invisibly. ``/anilist login`` did exactly that in production on
2026-08-25 03:23: it DMs the invoker (a DM CHANNEL has to be opened first, then
the message sent - two round trips) and only then called ``ctx.send``.

WHY IT IS A WALKER AND NOT A GREP
---------------------------------
Two cheaper sweeps were tried first and both were worthless:

* "every command whose first await is not a response" flagged 239 of 232
  callbacks - it counted a one-millisecond ``pool.fetchval`` as danger.
* "...whose first NETWORK-shaped await is not a response" flagged ZERO, and could
  not even see ``anilist_login``. The cause is that ``ast.walk`` is a
  breadth-first walk of the TREE, not of the execution. For ``anilist_login`` the
  first ``Await`` it yields is the trailing ``ctx.send("Check your DMs.")``,
  which is the LAST statement to run, and an early-return guard's ``ctx.send``
  yields before the happy path too. Read in tree order the command looks like it
  answers immediately; read in execution order it answers third.

So this walks the body the way Python runs it:

1. **Execution order.** :func:`ordered_awaits` yields awaits depth-first and
   left-to-right within an expression, so ``await f(await g())`` reports ``g``
   first, and a trailing statement reports last.
2. **Path-aware, guard-proof.** Branches are explored as separate paths and a
   path is PRUNED the moment it answers. An early-return guard that replies is
   therefore not "the command answered"; it is one path answering, while the
   fall-through path keeps being walked.
3. **Cost, not shape.** Each await is classified as an answer, a real round trip
   (a DM, an HTTP call, an executor render, a member fetch, a gateway member
   query) or something local (a Postgres query on loopback, an in-process map).
   An unrecognised call is followed INTO its body - through ``self._helper``,
   ``module.helper``, ``Paginator().start``, a base class, a ``get_cog()`` seam -
   and costed there. What still cannot be resolved is reported as UNKNOWN rather
   than assumed cheap, so the silence this guard produces is honest.

WHAT IT DELIBERATELY DOES NOT KNOW (the limits, stated)
-------------------------------------------------------
* A round trip reached through a callee is counted once, even when the callee
  loops over it. The census undercounts; it never overcounts.
* ``async with self._lock`` is assumed cheap. A lock held across a slow await
  elsewhere would not be seen.
* ``getattr(obj, "fetch_member")`` and other dynamic dispatch is UNKNOWN.
* An ALIASED send - ``dm = ctx.author.send`` then ``await dm(...)`` - is not
  recognised: the cost table keys on the attribute chain at the call site, so a
  send stored in a local variable reads as an UNKNOWN call, not a round trip.
  (The same blind spot as ``tests/test_errors_send_hygiene.py``.) Nothing in the
  tree writes that shape, and an unresolvable call is still reported as UNKNOWN
  rather than assumed cheap, so it degrades to "unknown", never to "safe".
* A ``for``/``while`` ``else`` body is walked for its COST but its outcome is
  discarded: a ``break`` skips it, so an answer in there cannot be trusted to
  have run. It over-reports rather than under-reports.
* A prefix-only command cannot expire anything and is not scanned.
"""

import ast
import pathlib

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SCAN_DIRS = ("cogs", "tools")

# ---------------------------------------------------------------------------
# Path outcomes
# ---------------------------------------------------------------------------
ALIVE = "ALIVE"        # unanswered, keep walking this path
ANSWERED = "ANSWERED"  # the interaction was answered: safe, prune
REPORTED = "REPORTED"  # a round trip was recorded on this path
EXITED = "EXITED"      # return/raise without answering

RESPONSE = "RESPONSE"
SLOW = "SLOW"
FAST = "FAST"
UNKNOWN = "UNKNOWN"

# Methods on the Context / Interaction binding that ANSWER the interaction.
# ``ctx.typing()`` is here on purpose: on a slash invocation discord.py's
# DeferTyping IS the defer (it is a no-op typing indicator on prefix).
RESPONSE_METHODS = frozenset({"send", "reply", "defer", "typing", "send_help"})

# Tail method name -> a real round trip.
SLOW_METHODS = frozenset(
    {
        # -- discord REST ---------------------------------------------------
        "fetch_member", "fetch_members", "fetch_user", "fetch_channel",
        "fetch_message", "fetch_guild", "fetch_roles", "fetch_ban", "fetch_bans",
        "fetch_emojis", "fetch_invites", "fetch_webhooks", "fetch_channels",
        "fetch_archived_threads", "fetch_permissions", "fetch_scheduled_events",
        "create_dm", "create_text_channel", "create_voice_channel",
        "create_thread", "create_role", "create_webhook", "create_invite",
        "create_forum",
        "add_roles", "remove_roles", "edit_roles", "edit",
        "ban", "unban", "kick", "timeout", "move_to", "delete",
        "purge", "chunk", "query_members", "pins", "clone",
        "set_permissions", "add_reaction", "remove_reaction", "clear_reactions",
        "start_thread", "add_user", "remove_user",
        # -- HTTP -----------------------------------------------------------
        "get_json", "post_json", "request", "graphql", "json", "text",
        # -- CDN / attachment ------------------------------------------------
        "read", "save_file", "to_file",
        # -- executor / thread offload ---------------------------------------
        "run_in_executor", "to_thread",
        # -- lavalink track resolution (leaves the box: YouTube, Spotify) ----
        "get_tracks", "search", "load_tracks",
    }
)

# ext.commands converters that fall back to a GATEWAY query_members on a cache
# miss and then wait on the chunk reply under wait_for(timeout=30.0). A miss is
# the NORM here: core.py sets chunk_guilds_at_startup=False. Every other
# converter in the tree is pure CPU over the cache.
QUERYING_CONVERTERS = frozenset({"MemberConverter", "UserConverter"})

# Sub-millisecond: local Postgres, in-process state, or a Lavalink control PATCH
# to the node on loopback.
FAST_METHODS = frozenset(
    {
        "acquire", "release", "fetch", "fetchrow", "fetchval", "fetchmany",
        "execute", "executemany", "copy_records_to_table", "transaction",
        "put", "put_nowait", "get_nowait", "flush",
        "pause", "resume", "seek", "set_volume", "set_pause", "set_filters",
        "stop", "play", "disconnect",
    }
)

# Awaits whose cost is the worst cost of their ARGUMENTS.
PASSTHROUGH = frozenset({"gather", "wait_for", "shield", "as_completed"})


def dotted(node):
    """``self.bot.db_pool.fetchrow`` / ``Paginator().start`` for a chain, or None."""

    parts = []
    cur = node
    while True:
        if isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        elif isinstance(cur, ast.Call):
            inner = dotted(cur.func)
            if inner is None:
                return None
            parts.append(f"{inner}()")
            break
        elif isinstance(cur, ast.Name):
            parts.append(cur.id)
            break
        elif isinstance(cur, ast.Subscript):
            inner = dotted(cur.value)
            parts.append(f"{inner}[]" if inner else "[]")
            break
        else:
            return None
    return ".".join(reversed(parts))


def ordered_awaits(node):
    """Yield the ``ast.Await`` nodes of one expression in EVALUATION order.

    Depth-first, left to right - the order the interpreter uses. This is the
    piece ``ast.walk`` gets wrong (see the module docstring).
    """

    if node is None:
        return
    if isinstance(node, ast.Await):
        yield from ordered_awaits(node.value)
        yield node
        return
    if isinstance(node, ast.Call):
        yield from ordered_awaits(node.func)
        for arg in node.args:
            yield from ordered_awaits(arg)
        for kw in node.keywords:
            yield from ordered_awaits(kw.value)
        return
    if isinstance(node, ast.BoolOp):
        for value in node.values:
            yield from ordered_awaits(value)
        return
    if isinstance(node, ast.IfExp):
        yield from ordered_awaits(node.test)
        yield from ordered_awaits(node.body)
        yield from ordered_awaits(node.orelse)
        return
    if isinstance(node, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return  # a nested def does not run here
    for child in ast.iter_child_nodes(node):
        yield from ordered_awaits(child)


class Index:
    """Every module in the tree, so a call can be followed into its body."""

    def __init__(self):
        self.modules = {}
        self.classes = {}
        self.functions = {}
        self.imports = {}
        self.method_owner = {}  # bare method name -> [(module, class), ...]

    def load(self, root=_REPO_ROOT, dirs=_SCAN_DIRS):
        for directory in dirs:
            for path in sorted((root / directory).rglob("*.py")):
                key = ".".join(path.relative_to(root).with_suffix("").parts)
                try:
                    tree = ast.parse(path.read_text(encoding="utf-8"))
                except SyntaxError:  # pragma: no cover - the repo compiles in CI
                    continue
                self.add(key, tree)
        for module, classes in self.classes.items():
            for cname, cdef in classes.items():
                for node in cdef.body:
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        self.method_owner.setdefault(node.name, []).append((module, cname))
        return self

    def add(self, key, tree):
        self.modules[key] = tree
        self.classes[key] = {}
        self.functions[key] = {}
        self.imports[key] = {}
        pkg = key.rsplit(".", 1)[0]
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                self.classes[key][node.name] = node
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.functions[key][node.name] = node
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    self.imports[key][alias.asname or alias.name.split(".")[0]] = alias.name
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base = pkg
                    for _ in range(node.level - 1):
                        base = base.rsplit(".", 1)[0]
                    if node.module:
                        base = f"{base}.{node.module}"
                else:
                    base = node.module or ""
                for alias in node.names:
                    self.imports[key][alias.asname or alias.name] = f"{base}.{alias.name}"


class Detector:
    """Scans an :class:`Index` for commands that answer too late."""

    def __init__(self, index):
        self.index = index
        self._cost_memo = {}
        self._busy = set()

    # -- resolution ---------------------------------------------------------
    def find_class(self, module, bare):
        cdef = self.index.classes.get(module, {}).get(bare)
        if cdef is not None:
            return module, cdef
        target = self.index.imports.get(module, {}).get(bare)
        if target:
            mod, _, attr = target.rpartition(".")
            cdef = self.index.classes.get(mod, {}).get(attr)
            if cdef is not None:
                return mod, cdef
        for mod, classes in self.index.classes.items():
            if bare in classes:
                return mod, classes[bare]
        return None

    def lookup_method(self, module, klass, attr, seen=None):
        seen = set() if seen is None else seen
        for node in klass.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == attr:
                return module, klass, node
        for base in klass.bases:  # mixins carry half of this cog tree
            bname = dotted(base)
            if not bname:
                continue
            bare = bname.split(".")[-1]
            if bare in seen:
                continue
            seen.add(bare)
            found = self.find_class(module, bare)
            if found:
                got = self.lookup_method(found[0], found[1], attr, seen)
                if got:
                    return got
        return None

    def resolve(self, module, klass, name):
        """``self._x`` / ``helpers.y`` / ``Paginator().start`` -> its def, or None."""

        parts = name.split(".")
        if len(parts) == 2 and parts[0] == "self" and klass is not None:
            got = self.lookup_method(module, klass, parts[1])
            if got:
                return got
        if len(parts) == 1:
            fn = self.index.functions.get(module, {}).get(parts[0])
            if fn is not None:
                return module, None, fn
            target = self.index.imports.get(module, {}).get(parts[0])
            if target:
                mod, _, attr = target.rpartition(".")
                fn = self.index.functions.get(mod, {}).get(attr)
                if fn is not None:
                    return mod, None, fn
        if len(parts) == 2:
            head, attr = parts
            target = self.index.imports.get(module, {}).get(head)
            if target:
                fn = self.index.functions.get(target, {}).get(attr)
                if fn is not None:
                    return target, None, fn
            found = self.find_class(module, head[:-2] if head.endswith("()") else head)
            if found:
                got = self.lookup_method(found[0], found[1], attr)
                if got:
                    return got
        # last resort: a method name owned by exactly ONE class in the tree.
        # This is what follows ``cog.cmd_give`` through a get_cog() seam.
        owners = self.index.method_owner.get(parts[-1], [])
        if len(parts) >= 2 and len(owners) == 1:
            mod, cname = owners[0]
            return self.lookup_method(mod, self.index.classes[mod][cname], parts[-1])
        return None

    # -- classification -----------------------------------------------------
    def classify(self, module, klass, ctxs, awaited, depth=0, aliases=None):
        inner = awaited.value if isinstance(awaited, ast.Await) else awaited
        call = inner if isinstance(inner, ast.Call) else None
        func = call.func if call is not None else inner
        name = dotted(func) or "<expr>"
        parts = name.split(".")
        if aliases and parts[0] in aliases:
            parts = aliases[parts[0]].split(".") + parts[1:]
            name = ".".join(parts)
        tail = parts[-1]

        # -- the interaction is answered ------------------------------------
        if len(parts) == 2 and parts[0] in ctxs and tail in RESPONSE_METHODS:
            return RESPONSE, name
        if parts[0] in ctxs and ("response" in parts or "followup" in parts):
            return RESPONSE, name
        if len(parts) >= 2 and parts[-2] == "response" and tail in (
            "send_message", "defer", "edit_message", "send_modal"
        ):
            return RESPONSE, name
        if len(parts) >= 2 and parts[-2] == "followup" and tail == "send":
            return RESPONSE, name
        if parts[0] == "interactions" and tail in ("defer", "reply", "notify_failure"):
            return RESPONSE, name

        # -- explicit costs --------------------------------------------------
        if tail in PASSTHROUGH and call is not None:
            worst = FAST
            for arg in list(call.args) + [kw.value for kw in call.keywords]:
                kind, label = self.classify(module, klass, ctxs, arg, depth, aliases)
                if kind == SLOW:
                    return SLOW, f"{name}(...{label}...)"
                if kind == UNKNOWN:
                    worst = UNKNOWN
            return worst, name
        if tail == "sleep":
            if call is not None and call.args:
                first = call.args[0]
                if isinstance(first, ast.Constant) and first.value == 0:
                    return FAST, name
            return SLOW, name
        if tail == "convert" and len(parts) >= 2:
            if parts[-2].rstrip("()") in QUERYING_CONVERTERS:
                return SLOW, name
        if tail in FAST_METHODS:
            return FAST, name
        if tail in SLOW_METHODS:
            return SLOW, name
        if tail == "send":
            # a send that is NOT on the ctx binding: a DM or another channel
            return SLOW, name

        # -- follow the call into its body ------------------------------------
        if depth < 8:
            target = self.resolve(module, klass, name)
            if target:
                mod, kls, fn = target
                return (
                    self.function_cost(mod, kls, fn, depth + 1),
                    f"{name} -> {mod}:{fn.lineno} {fn.name}",
                )
        return UNKNOWN, name

    def function_cost(self, module, klass, fn, depth=0):
        """The cost of CALLING this callee.

        SLOW when some path inside hits a round trip before answering,
        RESPONSE when EVERY exit answers (a helper that replies for you, e.g.
        ``Paginator.start``), FAST when nothing costly happens.
        """

        key = (module, klass.name if klass else None, fn.name, fn.lineno)
        if key in self._cost_memo:
            return self._cost_memo[key]
        if key in self._busy:
            return FAST  # recursion: no NEW cost from going round again
        self._busy.add(key)
        walker = Walker(self, module, klass, ctx_bindings(fn), depth=depth)
        if walker.block(fn.body) == ALIVE:
            walker.unresponded_exits += 1
        if walker.findings:
            kind = SLOW
        elif walker.answered and not walker.unresponded_exits:
            kind = RESPONSE
        elif walker.unknowns:
            kind = UNKNOWN
        else:
            kind = FAST
        self._busy.discard(key)
        self._cost_memo[key] = kind
        return kind

    # -- entry point --------------------------------------------------------
    def scan_callback(self, module, klass, fn):
        walker = Walker(self, module, klass, ctx_bindings(fn) or {"ctx"}, census=True)
        walker.block(fn.body)
        return walker


def ctx_bindings(fn):
    """The parameter names that ARE the interaction surface in this function."""

    args = [a.arg for a in fn.args.args]
    if args and args[0] in ("self", "cls"):
        args = args[1:]
    return {a for a in args if a in ("ctx", "context", "interaction", "itx")}


def slash_truth(test, ctxs):
    """Is ``test`` true on the SLASH path? True / False / None (unknown).

    A hybrid command runs both ways, and only ``ctx.interaction`` tells them
    apart: it is None for a prefix call, so ``if ctx.interaction:`` bodies are
    slash-only and ``if ctx.interaction is None:`` bodies are prefix-only.

    ``ctx.message`` is deliberately NOT a discriminator, and reading it as one
    was a bug in an earlier draft of this file. ext.commands fabricates a
    SYNTHETIC Message for an application-command Context
    (``Context.from_interaction`` builds one carrying the interaction id when
    ``interaction.message`` is None), so ``ctx.message is not None`` is TRUE on
    both paths - and a ``ctx.message.delete()`` behind such a guard really does
    fire a REST call on the slash path. It 404s, which costs the same wall clock
    as one that works. Treating that guard as prefix-only hid a round trip.
    """

    if isinstance(test, ast.BoolOp):
        values = [slash_truth(v, ctxs) for v in test.values]
        if isinstance(test.op, ast.And):
            if any(v is False for v in values):
                return False
            return True if all(v is True for v in values) else None
        if any(v is True for v in values):
            return True
        return False if all(v is False for v in values) else None
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        inner = slash_truth(test.operand, ctxs)
        return None if inner is None else (not inner)
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        left = dotted(test.left)
        right = test.comparators[0]
        if left is None or not (isinstance(right, ast.Constant) and right.value is None):
            return None
        parts = left.split(".")
        if len(parts) != 2 or parts[0] not in ctxs or parts[1] not in ("interaction", "message"):
            return None
        present = True  # both are set on the slash path (message is synthetic)
        if parts[1] == "message":
            present = True
        if isinstance(test.ops[0], ast.Is):  # ctx.X is None
            return not present
        if isinstance(test.ops[0], ast.IsNot):  # ctx.X is not None
            return present
        return None
    name = dotted(test)
    if name:
        parts = name.split(".")
        if len(parts) == 2 and parts[0] in ctxs and parts[1] in ("interaction", "message"):
            return True
    return None


class Walker:
    """Execution-order walk of a body. A path is pruned the moment it answers."""

    def __init__(self, detector, module, klass, ctxs, depth=0, census=False):
        self.detector = detector
        self.module = module
        self.klass = klass
        self.ctxs = ctxs
        self.depth = depth
        # census=True keeps walking past the first round trip, so a report can
        # say how MUCH runs before the answer. Off while costing a callee, where
        # the first hit already decides the verdict.
        self.census = census
        self.findings = []
        self.unknowns = []
        self.locks = []
        self.trace = []
        self.answered = 0
        self.unresponded_exits = 0
        self.loop_depth = 0
        self.aliases = {}  # local name -> "ClassName()" for x = ClassName(...)

    # -- helpers ------------------------------------------------------------
    def _cost(self, node):
        return self.detector.classify(
            self.module, self.klass, self.ctxs, node, self.depth, self.aliases
        )

    def _learn(self, st):
        """Remember ``converter = MemberConverter()`` so the call site resolves."""

        if not isinstance(st, (ast.Assign, ast.AnnAssign)):
            return
        value = st.value
        if not isinstance(value, ast.Call):
            return
        cname = dotted(value.func)
        if not cname:
            return
        targets = st.targets if isinstance(st, ast.Assign) else [st.target]
        for target in targets:
            if isinstance(target, ast.Name):
                self.aliases[target.id] = f"{cname.split('.')[-1]}()"

    def _loop_else(self, st):
        """Walk a ``for/else`` or ``while/else`` body for its COST only.

        The ``orelse`` of a loop runs when the loop ended without ``break`` - so
        a round trip in there really does happen before whatever follows, and
        skipping it was a blind spot (an ``else: await session.get_json(...)``
        was invisible). Its outcome is deliberately NOT propagated: a ``break``
        path skips the else entirely, so an answer inside it cannot prune the
        rest of the function. Cost is recorded, safety is never assumed.
        """

        if getattr(st, "orelse", None):
            self.block(st.orelse)

    def _merge(self, *outs):
        """Combine branch outcomes: ALIVE wins - some path continues past here."""

        if ALIVE in outs:
            return ALIVE
        if outs and all(o == ANSWERED for o in outs):
            return ANSWERED
        if REPORTED in outs:
            return REPORTED
        return EXITED

    # -- the walk -----------------------------------------------------------
    def block(self, stmts):
        for st in stmts:
            self._learn(st)
            out = self.stmt(st)
            if out != ALIVE:
                return out
        return ALIVE

    def exprs(self, *nodes):
        for node in nodes:
            for await_node in ordered_awaits(node):
                kind, label = self._cost(await_node)
                self.trace.append((await_node.lineno, kind, label))
                if kind == RESPONSE:
                    self.answered += 1
                    return ANSWERED
                if kind == SLOW:
                    self.findings.append((await_node.lineno, label, self.loop_depth))
                    if not self.census:
                        return REPORTED
                elif kind == UNKNOWN:
                    self.unknowns.append((await_node.lineno, label))
        return ALIVE

    def stmt(self, st):
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return ALIVE
        if isinstance(st, ast.If):
            out = self.exprs(st.test)
            if out != ALIVE:
                return out
            truth = slash_truth(st.test, self.ctxs)
            if truth is True:
                return self.block(st.body)
            if truth is False:
                return self.block(st.orelse) if st.orelse else ALIVE
            return self._merge(
                self.block(st.body),
                self.block(st.orelse) if st.orelse else ALIVE,
            )
        if isinstance(st, ast.Try):
            outs = [self.block(st.body)]
            for handler in st.handlers:
                outs.append(self.block(handler.body))
            out = self._merge(*outs)
            if out == ALIVE and st.orelse:
                out = self.block(st.orelse)
            if out == ALIVE and st.finalbody:
                out = self.block(st.finalbody)
            return out
        if isinstance(st, (ast.For, ast.AsyncFor)):
            if isinstance(st, ast.AsyncFor):
                kind, label = self._cost(st.iter)
                if kind == SLOW:
                    self.findings.append((st.lineno, f"async for {label}", self.loop_depth))
                    if not self.census:
                        return REPORTED
                elif kind == UNKNOWN:
                    self.unknowns.append((st.lineno, f"async for {label}"))
            else:
                out = self.exprs(st.iter)
                if out != ALIVE:
                    return out
            self.loop_depth += 1
            self.block(st.body)
            self.loop_depth -= 1
            self._loop_else(st)
            return ALIVE  # the zero-iteration path always exists
        if isinstance(st, ast.While):
            out = self.exprs(st.test)
            if out != ALIVE:
                return out
            self.loop_depth += 1
            self.block(st.body)
            self.loop_depth -= 1
            self._loop_else(st)
            return ALIVE
        if isinstance(st, (ast.With, ast.AsyncWith)):
            for item in st.items:
                if isinstance(st, ast.AsyncWith):
                    expr = item.context_expr
                    if not isinstance(expr, ast.Call):
                        self.locks.append((st.lineno, dotted(expr) or "<expr>"))
                        continue
                    kind, label = self._cost(expr)
                    self.trace.append((st.lineno, kind, f"async with {label}"))
                    if kind == RESPONSE:
                        self.answered += 1
                        return ANSWERED
                    if kind == SLOW:
                        self.findings.append((st.lineno, f"async with {label}", self.loop_depth))
                        if not self.census:
                            return REPORTED
                    elif kind == UNKNOWN:
                        self.unknowns.append((st.lineno, f"async with {label}"))
                else:
                    out = self.exprs(item.context_expr)
                    if out != ALIVE:
                        return out
            return self.block(st.body)
        if isinstance(st, ast.Return):
            out = self.exprs(st.value)
            if out != ALIVE:
                return out
            self.unresponded_exits += 1
            return EXITED
        if isinstance(st, ast.Raise):
            out = self.exprs(st.exc)
            if out != ALIVE:
                return out
            self.unresponded_exits += 1
            return EXITED
        if isinstance(st, (ast.Break, ast.Continue)):
            return EXITED
        if isinstance(st, ast.Pass):
            return ALIVE
        if isinstance(st, ast.Match):
            out = self.exprs(st.subject)
            if out != ALIVE:
                return out
            outs = [self.block(case.body) for case in st.cases]
            wildcard = any(
                isinstance(case.pattern, ast.MatchAs) and case.pattern.pattern is None
                for case in st.cases
            )
            if not wildcard:
                outs.append(ALIVE)
            return self._merge(*outs)
        parts = []
        for _field, value in ast.iter_fields(st):
            if isinstance(value, ast.AST):
                parts.append(value)
            elif isinstance(value, list):
                parts.extend(x for x in value if isinstance(x, ast.expr))
        return self.exprs(*parts)


# ---------------------------------------------------------------------------
# Command discovery
# ---------------------------------------------------------------------------
def _decorator_name(dec):
    node = dec.func if isinstance(dec, ast.Call) else dec
    return dotted(node) or ""


def discover(index):
    """Every SLASH-reachable callback: hybrid commands, groups, subcommands.

    A ``@commands.command`` / ``@commands.group`` (and anything hanging off a
    plain group, e.g. ``?purges``) is prefix-only: there is no interaction and
    nothing to expire, so it is not scanned.
    """

    slash_owners = set()
    for tree in index.modules.values():
        for klass in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
            for fn in klass.body:
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for name in (_decorator_name(d) for d in fn.decorator_list):
                    if name.endswith("hybrid_group"):
                        slash_owners.add(fn.name)
                    elif name.endswith(".group") and name.rsplit(".", 2)[-2] in slash_owners:
                        slash_owners.add(fn.name)
    found = []
    for module, tree in index.modules.items():
        if not module.startswith("cogs."):
            continue
        for klass in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
            for fn in klass.body:
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                kind = None
                for name in (_decorator_name(d) for d in fn.decorator_list):
                    if name.endswith("hybrid_command") or name.endswith("hybrid_group"):
                        kind = "hybrid"
                    elif name.endswith("app_commands.command"):
                        kind = "app"
                    elif (
                        name.endswith(".command") or name.endswith(".group")
                    ) and name.rsplit(".", 2)[-2] in slash_owners:
                        kind = kind or "sub"
                if kind:
                    found.append((module, klass, fn))
    return found


# ---------------------------------------------------------------------------
# The accepted list
# ---------------------------------------------------------------------------
# Commands that DO reach a round trip before answering and are deliberately left
# that way. Each entry states the count the walker measured and why that cost is
# not a 10062 risk worth changing the UX for. A defer is not free: it puts a
# "thinking" state on every single invocation, and it forces an ephemeral choice
# that every later send has to honour, so it is spent where the wait is
# structural - a DM, a third-party API, an image render, a gateway member query,
# a loop - not where it is one bounded Discord REST call.
#
# The shape is: "module:Class.callback": (reason, round_trips_measured).
ACCEPTED = {
    # -- one bounded Discord REST call, behind a moderator-rate command -------
    "cogs.moderation.moderation:Moderation._ban": (
        "one REST GET /guilds/{id}/members/{user}, and only on a cache miss "
        "(modchecks.hierarchy_error_resolved); the ban itself happens after "
        "_confirm has already answered",
        1,
    ),
    "cogs.moderation.moderation:Moderation._kick": (
        "the same cache-miss member fetch, then guild.kick - two bounded REST "
        "calls on a command already rate-limited to one per five seconds",
        2,
    ),
    "cogs.moderation.moderation:Moderation._unban": (
        "one guild.unban", 1,
    ),
    "cogs.moderation.moderation:Moderation._move": (
        "one member.move_to (a voice-state PATCH)", 1,
    ),
    "cogs.moderation.moderation:Moderation._voicekick": (
        "one member.move_to(None)", 1,
    ),
    "cogs.moderation.moderation:Moderation.mute": (
        "one member.add_roles; the expensive first-use branch (create the role, "
        "then walk every channel) already answers with 'Mute role is not "
        "defined' BEFORE it starts",
        1,
    ),
    "cogs.moderation.moderation:Moderation.unmute": (
        "one member.remove_roles", 1,
    ),
    "cogs.moderation.moderation:Moderation.moverole": (
        "one role.edit", 1,
    ),
    "cogs.moderation.moderation:Moderation.tempban": (
        "the worst of the moderation family - a cache-miss member fetch, the "
        "ban, and the modlog post; the unban in the census is the rollback leg, "
        "which only runs when scheduling FAILED. Still all bounded REST, and a "
        "defer here would put a thinking state on every ban in the server. "
        "The first candidate to revisit if a 10062 ever shows up in the log",
        4,
    ),
    "cogs.moderation.warns:Warns.warn": (
        "one REST call, and only when a warn crosses an escalation threshold "
        "(timeout/kick/ban); the no-rule path answers with no round trip at all",
        1,
    ),
    # -- one REST call on an admin setup command -----------------------------
    "cogs.config.tickets.panel:Tickets.ticket_setup": (
        "one channel.send to post the panel; the writes around it are local "
        "Postgres",
        1,
    ),
    "cogs.config.verification:Verification.verify_setup": (
        "one channel.send to post the verify panel", 1,
    ),
    "cogs.config.twitch:Twitch.twitch_addrole": (
        "one guild.create_role, and only when the configured role is missing", 1,
    ),
    "cogs.config.twitch:Twitch.twitch_removerole": (
        "one role.delete", 1,
    ),
    "cogs.config.buttonroles:ButtonRoles.buttonrole_remove": (
        "channel.fetch_message then message.edit to strip the buttons - two "
        "bounded REST calls, both best-effort (a failure is swallowed)",
        2,
    ),
    "cogs.utility.info:Info.info_user": (
        "one bot.fetch_user for the banner; capture_banner is handed the user "
        "we JUST fetched (fetched=full) precisely so it does not fetch twice",
        2,
    ),
    # -- music: a controller repost, on a loopback-adjacent path --------------
    "cogs.music.music:Music.play": (
        "only the bare `/play` with a live session, which reposts the "
        "controller (delete + send). `/play <query>` already defers - the "
        "picker defers before searching, and the classic path defers before "
        "_play_query. Deferring the bare form would have to be ephemeral to "
        "match its ephemeral ack, and that would break the public vibe/join "
        "cards the same branch sends",
        1,
    ),
    "cogs.music.music:Music.nowplaying": (
        "the same controller repost as bare /play (delete + send)", 1,
    ),
    "cogs.music.music:Music.skip": (
        "posting or joining a public skip vote - one channel send", 1,
    ),
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def scan():
    index = Index().load()
    detector = Detector(index)
    return {
        f"{module}:{klass.name}.{fn.name}": detector.scan_callback(module, klass, fn)
        for module, klass, fn in discover(index)
    }


def _snippet(source, func="cmd", klass=None, ctxs=None):
    """Run the walker over one inline function, with no repo index behind it."""

    index = Index()
    index.add("snippet", ast.parse(source))
    detector = Detector(index)
    tree = index.modules["snippet"]
    if klass:
        cdef = index.classes["snippet"][klass]
        fn = next(n for n in cdef.body if getattr(n, "name", None) == func)
        return detector.scan_callback("snippet", cdef, fn)
    fn = next(n for n in ast.walk(tree) if getattr(n, "name", None) == func)
    walker = Walker(detector, "snippet", None, ctxs or ctx_bindings(fn) or {"ctx"}, census=True)
    walker.block(fn.body)
    return walker


# ---------------------------------------------------------------------------
# Calibration: the ONE case we have production proof of
# ---------------------------------------------------------------------------
# The shape of cogs/anilist/account.py::anilist_login as it was when it raised
# 404 10062 on 2026-08-25 03:23. Every trap that defeated the earlier sweeps is
# in here: a guard clause that answers and returns, a DM in a try, an except
# handler that answers, and a trailing ctx.send.
_ANILIST_LOGIN_BEFORE = '''
async def anilist_login(self, ctx):
    if not self._login_available():
        return await ctx.send("not configured")
    instructions = self._login_instructions()
    view = LoginView(self, ctx.author.id)
    try:
        view.message = await ctx.author.send(instructions, view=view)
    except discord.Forbidden:
        view.message = await ctx.send(instructions, view=view, ephemeral=True)
        return
    await ctx.send("Check your DMs.")
'''


def test_calibration_detector_flags_the_proven_case():
    """The known positive: the pre-fix anilist_login shape, at the DM line."""

    walker = _snippet(_ANILIST_LOGIN_BEFORE, func="anilist_login")

    assert [(line, label) for line, label, _loop in walker.findings] == [
        (8, "ctx.author.send")
    ], "the DM is the round trip, and it is the ONLY one before the answer"


def test_calibration_the_shipped_command_is_clean(scan):
    """...and the shipped command, with its defer, is not flagged any more."""

    walker = scan["cogs.anilist.account:AccountMixin.anilist_login"]

    assert walker.findings == []
    assert walker.trace[0][1] == RESPONSE, walker.trace[:3]


def test_ast_walk_yields_the_wrong_await_first():
    """Pin the reason a tree-order sweep saw nothing: it reads the LAST send first."""

    tree = ast.parse(_ANILIST_LOGIN_BEFORE)
    fn = next(n for n in ast.walk(tree) if getattr(n, "name", None) == "anilist_login")

    tree_order = [
        dotted(n.value.func) for n in ast.walk(fn) if isinstance(n, ast.Await)
    ]
    run_order = [dotted(n.value.func) for n in ordered_awaits_of_body(fn)]

    # ast.walk hands back the trailing "Check your DMs." send first...
    assert tree_order[0] == "ctx.send"
    # ...while execution order starts with the guard, then the DM.
    assert run_order[:2] == ["ctx.send", "ctx.author.send"]
    assert run_order[-1] == "ctx.send"


def ordered_awaits_of_body(fn):
    for st in fn.body:
        yield from ordered_awaits(st)


def test_a_nested_await_is_yielded_before_the_call_it_feeds():
    """``await ctx.send(await slow())`` runs ``slow()`` FIRST.

    The inner-before-outer rule is the whole of "execution order" - reverse it
    and a round trip hidden inside a response's own arguments reads as if the
    response came first, which is the mistake that made a tree-order sweep
    report zero. Asserted on the yield order itself, not only on a flat body,
    because a flat body cannot tell the two orders apart.
    """

    tree = ast.parse("async def cmd(ctx):\n    await ctx.send(await ctx.guild.fetch_member(1))\n")
    fn = tree.body[0]

    assert [dotted(a.value.func) for a in ordered_awaits_of_body(fn)] == [
        "ctx.guild.fetch_member",
        "ctx.send",
    ]


def test_call_arguments_are_ordered_left_to_right():
    """Two round trips in one call still report in the order they run."""

    tree = ast.parse(
        "async def cmd(ctx):\n"
        "    await ctx.send(await ctx.guild.fetch_member(1), await ctx.bot.fetch_user(2))\n"
    )
    fn = tree.body[0]

    assert [dotted(a.value.func) for a in ordered_awaits_of_body(fn)] == [
        "ctx.guild.fetch_member",
        "ctx.bot.fetch_user",
        "ctx.send",
    ]


def test_a_round_trip_inside_the_response_arguments_is_still_flagged():
    """The same rule, end to end: the fetch happens before the send is sent."""

    walker = _snippet(
        """
async def cmd(self, ctx):
    await ctx.send(await ctx.guild.fetch_member(1))
"""
    )
    assert [label for _l, label, _d in walker.findings] == ["ctx.guild.fetch_member"]


# ---------------------------------------------------------------------------
# The walker's own rules
# ---------------------------------------------------------------------------
def test_a_guard_clause_that_answers_does_not_mask_the_happy_path():
    walker = _snippet(
        """
async def cmd(self, ctx, target):
    if target is None:
        return await ctx.send("no target")
    await ctx.author.send("hi")
    await ctx.send("done")
"""
    )
    assert [label for _l, label, _d in walker.findings] == ["ctx.author.send"]


def test_a_defer_before_the_round_trip_clears_it():
    walker = _snippet(
        """
async def cmd(self, ctx):
    await ctx.defer(ephemeral=True)
    await ctx.author.send("hi")
    await ctx.send("done")
"""
    )
    assert walker.findings == []


def test_ctx_typing_counts_as_the_answer():
    """On a slash invocation discord.py's DeferTyping IS the defer."""

    walker = _snippet(
        """
async def cmd(self, ctx):
    async with ctx.typing():
        await ctx.author.send("hi")
"""
    )
    assert walker.findings == []


def test_a_local_postgres_read_is_not_a_round_trip():
    walker = _snippet(
        """
async def cmd(self, ctx):
    row = await self.bot.db_pool.fetchrow("SELECT 1")
    await ctx.send(str(row))
"""
    )
    assert walker.findings == []


def test_the_ctx_message_guard_is_not_read_as_prefix_only():
    """The synthetic-Message trap: this delete DOES run on the slash path."""

    walker = _snippet(
        """
async def cmd(self, ctx):
    if ctx.message is not None:
        await ctx.message.delete()
    await ctx.send("done")
"""
    )
    assert [label for _l, label, _d in walker.findings] == ["ctx.message.delete"]


def test_an_interaction_guard_is_read_as_prefix_only():
    walker = _snippet(
        """
async def cmd(self, ctx):
    if ctx.interaction is None:
        await ctx.message.add_reaction("ok")
    await ctx.send("done")
"""
    )
    assert walker.findings == []


def test_a_helper_that_answers_ends_the_walk():
    walker = _snippet(
        """
class Cog:
    async def _reply(self, ctx):
        await ctx.send("hello")

    async def cmd(self, ctx):
        await self._reply(ctx)
        await ctx.author.send("a DM after the answer is fine")
""",
        func="cmd",
        klass="Cog",
    )
    assert walker.findings == []


def test_a_helper_that_dms_is_costed_through_the_call():
    walker = _snippet(
        """
class Cog:
    async def _notify(self, user):
        await user.send("hello")

    async def cmd(self, ctx):
        await self._notify(ctx.author)
        await ctx.send("done")
""",
        func="cmd",
        klass="Cog",
    )
    assert [label for _l, label, _d in walker.findings][0].startswith("self._notify ->")


def test_a_gateway_querying_converter_is_a_round_trip():
    """The rule ``addrole`` / ``removerole`` are deferred FOR.

    ``MemberConverter.convert`` does not read the cache and give up on a miss:
    it asks the GATEWAY (``ConnectionState.query_members``) and waits on the
    chunk reply under ``asyncio.wait_for(..., timeout=30.0)``. A miss is the norm
    (``core.py`` sets ``chunk_guilds_at_startup=False``), so a thirty-second wait
    sits inside a three-second window. Pinned here because the two commands'
    runtime tests would still pass with ``QUERYING_CONVERTERS`` emptied - the
    classification rule itself has to be held, not just its consequence.
    """

    walker = _snippet(
        """
async def cmd(self, ctx, member):
    converter = MemberConverter()
    m = await converter.convert(ctx, member)
    await ctx.send("done")
"""
    )
    assert [label for _l, label, _d in walker.findings] == [
        "MemberConverter().convert"
    ]


def test_a_cache_only_converter_is_not_a_round_trip():
    """Counter-test: the SLOW verdict comes from the converter list, not from
    the word ``convert``. Every other converter in the tree is pure CPU over the
    cache, and flagging them all would make the guard noise."""

    walker = _snippet(
        """
async def cmd(self, ctx, name):
    converter = RoleConverter()
    role = await converter.convert(ctx, name)
    await ctx.send("done")
"""
    )
    assert walker.findings == []


def test_a_round_trip_in_a_loop_else_is_seen():
    """``for/else`` runs its ``else`` when no ``break`` fired - real cost."""

    walker = _snippet(
        """
async def cmd(self, ctx, ids):
    for i in ids:
        if i:
            break
    else:
        await ctx.author.send("nothing matched")
    await ctx.send("done")
"""
    )
    assert [label for _l, label, _d in walker.findings] == ["ctx.author.send"]


def test_an_answer_in_a_loop_else_does_not_prune_the_rest():
    """...but an answer in there is not proof: a ``break`` skips the else.

    So the else is costed and its outcome dropped - the walk continues, and a
    later round trip is still reported. Over-reporting, never under-reporting.
    """

    walker = _snippet(
        """
async def cmd(self, ctx, ids):
    for i in ids:
        if i:
            break
    else:
        await ctx.send("nothing matched")
    await ctx.author.send("a DM")
    await ctx.send("done")
"""
    )
    assert [label for _l, label, _d in walker.findings] == ["ctx.author.send"]


def test_a_round_trip_in_a_while_else_is_seen():
    walker = _snippet(
        """
async def cmd(self, ctx, pages):
    while pages:
        pages.pop()
    else:
        await ctx.author.send("done paging")
    await ctx.send("done")
"""
    )
    assert [label for _l, label, _d in walker.findings] == ["ctx.author.send"]


# ---------------------------------------------------------------------------
# The guard itself
# ---------------------------------------------------------------------------
def test_every_slash_callback_answers_before_a_round_trip(scan):
    """No command may reach a round trip before answering unless it is ACCEPTED."""

    offenders = {name: w for name, w in scan.items() if w.findings}
    unexpected = {
        name: [f"L{line} {label}" for line, label, _loop in sorted(w.findings)]
        for name, w in offenders.items()
        if name not in ACCEPTED
    }

    assert not unexpected, (
        "these slash commands can burn the 3s interaction window before they "
        "answer - defer (ctx.defer(), a no-op on the prefix path) or add an "
        "entry to ACCEPTED with a written reason:\n"
        + "\n".join(f"  {k}: {v}" for k, v in sorted(unexpected.items()))
    )


def test_the_accepted_list_has_no_stale_entries(scan):
    """An ACCEPTED command that got fixed must lose its entry, not keep it."""

    stale = [
        name
        for name in ACCEPTED
        if name not in scan or not scan[name].findings
    ]

    assert not stale, (
        "these entries no longer describe anything - the command now answers "
        "first (or was renamed). Remove them:\n  " + "\n  ".join(sorted(stale))
    )


def test_the_scan_actually_covered_the_command_tree(scan):
    """A detector that silently scanned nothing would pass every test above."""

    assert len(scan) > 200, len(scan)
    # the four commands this run fixed must all be in the scanned set
    for name in (
        "cogs.anilist.account:AccountMixin.anilist_login",
        "cogs.anilist.account:AccountMixin.anilist_code",
        "cogs.config.welcome:Welcome.welcome_test",
        "cogs.moderation.moderation:Moderation.addrole",
    ):
        assert name in scan, name
