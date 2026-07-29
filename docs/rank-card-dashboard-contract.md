# Contrat rank_cards - dashboard (RC2, 2026-07-28)

Destinataire: l'equipe dashboard (Node/Remix), pour ecrire dans `rank_cards` sans
jamais produire une carte cassee ni une ligne que Postgres refuse. Source de
verite bot-side: `tools/rank_card.py` (geometrie, caps, formats) + le seam
`cogs/community/leveling.py` (`set_rank_background` / `set_rank_accent` /
`clear_rank_card`). Le dashboard N'A PAS ce seam (process separe) - il ecrit la
ligne directement, PUIS notifie (voir plus bas). Ce document decrit exactement
ce que le dashboard doit reproduire pour rester en phase.

## Table `rank_cards`

```sql
CREATE TABLE rank_cards (
    guild_id          BIGINT      PRIMARY KEY,
    background        BYTEA,                 -- WebP normalise, NULL = pas de fond
    background_format TEXT,                  -- 'webp' (NULL quand background est NULL)
    accent            INTEGER,               -- 0xRRGGBB, NULL = defaut (couleur de role du membre)
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT rank_cards_accent_range
        CHECK (accent IS NULL OR (accent >= 0 AND accent <= 16777215)),
    CONSTRAINT rank_cards_background_size
        CHECK (background IS NULL OR octet_length(background) <= 524288)
);
```

- `accent` est un entier pack 0xRRGGBB (identique a `discord.Colour.value`), PAS
  une chaine hex. `0..16777215` (`0xFFFFFF`) inclus; toute autre valeur est
  REFUSEE par la table (`rank_cards_accent_range`), pas seulement par le bot.
- `background` DOIT deja etre au format final (voir "Validation image" ci-dessous)
  avant l'INSERT/UPDATE. La table ne valide QUE la taille en octets
  (`rank_cards_background_size`, 524288 = 512 KiB) - elle ne verifie ni les
  dimensions, ni le format, ni que les octets sont un WebP valide. Une ligne qui
  passe le CHECK mais n'est pas un WebP 880x240 valide ne fera JAMAIS erreur cote
  DB: elle degradera silencieusement au rendu (voir "Ce qui se passe si vous
  desobeissez").
- `background_format` est toujours `'webp'` quand `background` est non NULL (le
  bot n'accepte/ne stocke jamais autre chose), et NULL quand `background` est
  NULL. Ecrire un `background` non NULL avec un `background_format` different de
  `'webp'` n'est pas bloque par la DB mais casse un invariant que le rendu
  suppose - ne le faites pas.

## Semantique des writes

Chaque colonne s'ecrit INDEPENDAMMENT (upsert single-column), jamais un
"set both or fail" - un serveur qui n'a configure qu'un accent doit garder son
fond si vous ne touchez qu'a l'accent, et vice-versa. Les deux requetes
canoniques (copiez-les, ne les reinventez pas):

```sql
-- Fond (accent inchange si deja present)
INSERT INTO rank_cards (guild_id, background, background_format, updated_at)
VALUES ($1, $2, $3, now())
ON CONFLICT (guild_id) DO UPDATE SET
    background = EXCLUDED.background,
    background_format = EXCLUDED.background_format,
    updated_at = now();

-- Accent (fond inchange si deja present)
INSERT INTO rank_cards (guild_id, accent, updated_at)
VALUES ($1, $2, now())
ON CONFLICT (guild_id) DO UPDATE SET
    accent = EXCLUDED.accent,
    updated_at = now();
```

Pour un reset:

```sql
-- Reset fond seul (garde l'accent)
UPDATE rank_cards SET background = NULL, background_format = NULL, updated_at = now()
WHERE guild_id = $1;

-- Reset accent seul (garde le fond)
UPDATE rank_cards SET accent = NULL, updated_at = now() WHERE guild_id = $1;

-- Reset complet (carte stock)
DELETE FROM rank_cards WHERE guild_id = $1;
```

`updated_at = now()` sur CHAQUE write, y compris un reset partiel - c'est la
seule piste d'audit ("quand ce serveur a-t-il change sa carte pour la derniere
fois") et le bot la met a jour de la meme facon a chaque appel du seam.

## Notification obligatoire apres CHAQUE write

Apres tout INSERT/UPDATE/DELETE sur `rank_cards` (fond OU accent, set OU reset),
le dashboard DOIT emettre:

```sql
SELECT pg_notify('yasuho_dashboard', '{"kind": "rank_card", "guildId": "<id>"}');
```

`guildId` est accepte en int ou en chaine numerique (le bot le parse en int
cote `cogs/system/dashboard_sync.py`). Sans cette notification, le bot continue
de servir sa version en cache (`Leveling._rank_cards`, jusqu'a 512 entrees, pas
de TTL) - potentiellement pour toujours si l'entree n'est jamais evincee sous
pression de cache. Ce n'est pas une notification "best effort": c'est la moitie
manquante du contrat que le bot respecte lui-meme cote seam (write + invalidation
dans le meme appel).

## Validation image (DOIT reproduire `tools/rank_card.validate_and_downscale`)

Le bot ne fait JAMAIS confiance a une ligne `rank_cards` au moment du rendu (pas
de re-validation cote lecture, cf. `Leveling._paint_background`: une ligne
illisible degrade silencieusement vers la carte stock plutot que de crasher
`/rank`) - c'est PRECISEMENT pourquoi la validation doit avoir lieu cote
ECRITURE, et donc cote dashboard si c'est lui qui ecrit. Le pipeline exact a
reproduire, dans cet ordre (le moins cher d'abord):

1. **Taille source** <= 8 MiB (`MAX_SOURCE_BYTES`) avant tout decodage.
2. **Format sniffe depuis les octets**, jamais depuis une extension ou un
   Content-Type declare par le client: seuls PNG, JPEG, WebP sont acceptes
   (`ACCEPTED_FORMATS`). Un format non reconnu est un refus, pas une conversion
   au mieux-effort.
3. **Plafond de pixels decodes**: 40 000 000 px (`MAX_SOURCE_PIXELS`), verifie
   AVANT le decodage complet (depuis les dimensions du header) - une bombe de
   decompression (PNG tres compressible qui explose a des dizaines de MP) doit
   etre refusee avant l'allocation, pas apres.
4. **Cover-crop vers exactement 880x240** (`CARD_WIDTH x CARD_HEIGHT`): mise a
   l'echelle par le plus grand des deux ratios puis crop centre - jamais de
   deformation, jamais un fond qui n'est pas EXACTEMENT 880x240. Un fond stocke
   a une autre taille sera redimensionne au vol par le renderer
   (`Leveling._paint_background` fait un resize defensif), ce qui coute un
   LANCZOS resample a CHAQUE `/rank` de ce serveur - le but de la validation a
   l'ecriture est justement d'eviter ce cout recurrent.
5. **Encodage WebP**, qualite 80, retente UNE fois a qualite 60 si le resultat
   depasse 512 KiB (`MAX_STORED_BYTES` - le meme plafond que le CHECK SQL). Si
   meme la passe degradee depasse la limite: refuser, ne jamais stocker une
   image tronquee ou une qualite degradee au-dela de ce second palier.

Le dashboard doit implementer ces 5 etapes cote Node (ou appeler un service qui
le fait) AVANT tout INSERT - PAS se reposer sur le CHECK SQL pour attraper une
image trop lourde: le CHECK ne verifie que la taille en octets finale, jamais
les dimensions ni le format. Une image qui n'est pas 880x240 WebP passera le
CHECK et degradera silencieusement au rendu (voir ci-dessous), une regression
invisible tant que personne ne regarde `/rank` sur ce serveur.

## Ce qui se passe si vous desobeissez

| Violation | Consequence |
|---|---|
| `background` > 512 KiB | INSERT/UPDATE rejete par `rank_cards_background_size` (erreur DB explicite) |
| `accent` hors 0..0xFFFFFF | INSERT/UPDATE rejete par `rank_cards_accent_range` (erreur DB explicite) |
| `background` pas un WebP valide (octets corrompus) | PAS d'erreur DB. Au rendu, `Leveling._paint_background` logge un warning et bascule sur le panneau stock - ce serveur perd silencieusement son fond a chaque `/rank` |
| `background` WebP valide mais pas 880x240 | PAS d'erreur DB. Redimensionne au vol a CHAQUE rendu (cout Pillow recurrent, pas une regression fonctionnelle mais une regression de cout) |
| `background_format` != 'webp' alors que `background` non NULL | PAS d'erreur DB. Le renderer suppose du WebP-compatible (Pillow decode via le sniff du header, donc un PNG/JPEG stocke ici SE DECODERA quand meme) - non teste, non garanti, a eviter |
| Write sans le `pg_notify('yasuho_dashboard', ...)` | Le bot sert la version en cache jusqu'a eviction (jusqu'a 512 entrees LRU, pas de TTL) - potentiellement indefiniment pour un serveur actif |

## Codes d'erreur suggeres (reponse API dashboard -> UI)

Alignes sur les exceptions typees de `tools/rank_card.py` pour que le message
utilisateur du dashboard et celui du bot (`/levelconfig card background`)
racontent la meme histoire face a la meme image:

| Code | Cause | Miroir bot (`RankCardError`) |
|---|---|---|
| `RANK_CARD_SOURCE_TOO_LARGE` | Upload > 8 MiB | `SourceTooLarge` |
| `RANK_CARD_IMAGE_TOO_LARGE` | > 40 MP decodes | `ImageTooLarge` |
| `RANK_CARD_UNSUPPORTED_FORMAT` | Format sniffe hors PNG/JPEG/WebP | `UnsupportedFormat` |
| `RANK_CARD_DECODE_FAILED` | Octets illisibles/corrompus | `DecodeFailed` |
| `RANK_CARD_ENCODED_TOO_LARGE` | Meme apres la passe degradee, > 512 KiB | `EncodedTooLarge` |
| `RANK_CARD_INVALID_ACCENT` | Hex hors 0..0xFFFFFF ou mal forme | `InvalidAccent` |

## References cote bot (pour le debug croise)

- `tools/rank_card.py` - geometrie (`CARD_WIDTH`/`CARD_HEIGHT`/`CARD_RADIUS`),
  caps, `validate_and_downscale`, `validate_accent`, les 4+3 requetes de
  stockage.
- `cogs/community/leveling.py` - le seam RC2 (`set_rank_background` /
  `set_rank_accent` / `clear_rank_card`) et le cache de style
  (`_rank_cards`, `ensure_rank_card_style`, `invalidate_rank_card`).
- `cogs/system/dashboard_sync.py` - `_invalidate_rank_card` (le cote bot de la
  notification `pg_notify`), `VALID_KINDS`, le format exact du payload.
- `schema.sql` - la definition canonique de la table (les deux CHECK ci-dessus).
