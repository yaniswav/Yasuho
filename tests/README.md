# Yasuho test suite

Fast, hermetic unit/import tests. They NEVER touch the network, a database,
Discord, or Lavalink - every external boundary is replaced by an in-memory
stand-in.

## Running

From the repo root:

```
python -m pytest
```

Install the test-only dependencies first if needed:

```
python -m pip install -r requirements-dev.txt
```

Configuration (`asyncio_mode = auto`, `testpaths = tests`, `pythonpath = .`,
importlib import mode) lives in `pytest.ini`, so no extra flags are required.

## What `conftest.py` provides

`conftest.py` runs at collection time, BEFORE any test imports `cogs` or
`core`. It does two import-time jobs and then exposes shared fixtures.

Import-time bootstrap:

- sonolink stub - the music backend needs Python 3.12+, so on the 3.10 dev box
  the real package is absent. The stub is injected into `sys.modules` ONLY when
  the real `sonolink` cannot be imported, so 3.12+ CI exercises the real package
  while local imports keep working. The fake defines every attribute
  `cogs/music/music.py` evaluates at import/def time.
- config bootstrap - if `config/bot.ini` or `config/tokens.ini` is missing, it
  is copied from the committed `*.template.ini` sibling (which contains every key
  read at import) so the `config_loader` singleton resolves. Existing local files
  are left untouched.

Fixtures:

- `fake_pool` - in-memory asyncpg pool stand-in; records every
  `execute`/`fetch`/`fetchrow`/`fetchval` call and returns configurable values.
- `make_interaction` - factory for a fake `discord.Interaction` that records
  `send_message`, `edit_message`, `defer`, `followup.send`, and `message.edit`.
- `make_context` - factory for a fake `commands.Context` with a recorded `send`.
- `crypto_key` - points `tools.crypto` at a fresh valid Fernet key and restores
  the module cache afterwards.
- `reset_locale` (autouse) - resets the i18n locale ContextVar to the default
  around every test so locale state never leaks between tests.

## `test_view_hygiene.py`

The highest-value guard in the suite. It defends against a real prod crash: a
`discord.ui.View` subclass defined a method named `_refresh`, which shadowed
discord.py's internal `BaseView._refresh(self, components)`. On the next
`MESSAGE_UPDATE` gateway event discord.py called the shadowing method with a
`components` list and the bot crashed. The fix was to rename it to `_rerender`.

The module:

1. Provides a pure `find_collisions(view_cls)` that reports methods a
   View/Modal/LayoutView subclass defines that collide with a discord.py
   internal attribute and are not documented override hooks.
2. Proves the guard is not vacuous: a synthetic subclass defining `_refresh` is
   flagged, a clean one is not, and `_refresh` really is a framework internal.
3. Imports every module under `cogs/` and `tools/`, collects every View-family
   subclass, and asserts zero collisions - failing with the exact offending
   `module.Class.method` if anyone reintroduces such a shadow.
