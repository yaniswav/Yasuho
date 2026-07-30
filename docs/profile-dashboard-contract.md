# Contrat profils sociaux - dashboard (P6, 2026-07-30)

Destinataire: l'equipe dashboard (Node/Remix), pour ecrire dans les trois tables
du profil social sans jamais produire une carte cassee, une fuite de visibilite
ni une ligne que Postgres refuse. Sources de verite cote bot:

- `cogs/community/profile/registry.py` - LE registre des champs: quels noms
  existent, quelles bornes, quelle whitelist de gamer IDs;
- `cogs/community/profile/visibility.py` - les trois niveaux et la regle
  "absent = private";
- `cogs/community/profile/storage.py` - la seule porte du bot vers
  `user_profiles` / `profile_visibility` (les requetes canoniques ci-dessous en
  sont la transcription);
- `cogs/community/profile/connectors/base.py` et `connectors/storage.py` - les
  caps et la seule porte vers `profile_connections`;
- `cogs/community/profile/presence.py` - le collecteur presence (P5), et la
  raison pour laquelle le dashboard ne cree JAMAIS un marker;
- `schema.sql` - la definition canonique des trois tables et de leurs CHECK.

Le dashboard n'a pas ces seams (process separe): il ecrit les lignes
directement. Ce document decrit exactement ce qu'il doit reproduire, et les
trois choses qu'il ne doit jamais faire.

Un mot sur le perimetre, parce qu'il differe du contrat `rank_cards`: un profil
est USER-scoped (une ligne par personne, pas de `guild_id` nulle part), et il
n'existe AUCUN cache DB partage cote bot pour ces tables. La consequence pratique
est plus loin ("Notifications"), et elle est bonne pour vous.

## Table `user_profiles`

```sql
CREATE TABLE user_profiles (
    user_id       BIGINT      PRIMARY KEY,
    bio           TEXT,
    pronouns      TEXT,
    accent        INTEGER,
    custom_fields JSONB       NOT NULL DEFAULT '[]'::jsonb,
    gaming_ids    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT user_profiles_accent_range
        CHECK (accent IS NULL OR (accent >= 0 AND accent <= 16777215)),
    CONSTRAINT user_profiles_bio_length
        CHECK (bio IS NULL OR char_length(bio) <= 300),
    CONSTRAINT user_profiles_pronouns_length
        CHECK (pronouns IS NULL OR char_length(pronouns) <= 40),
    CONSTRAINT user_profiles_custom_fields_shape
        CHECK (jsonb_typeof(custom_fields) = 'array'
               AND jsonb_array_length(custom_fields) <= 5),
    CONSTRAINT user_profiles_gaming_ids_shape
        CHECK (jsonb_typeof(gaming_ids) = 'object')
);
```

- `user_id` est la cle primaire ENTIERE: pas de `guild_id`, le profil suit la
  personne et pas le serveur.
- `accent` est un entier pack 0xRRGGBB (identique a `discord.Colour.value`), PAS
  une chaine hex. `0..16777215` inclus, toute autre valeur est refusee par
  `user_profiles_accent_range`. `accent = 0` est le NOIR, une vraie valeur:
  effacer l'accent, c'est ecrire NULL, jamais 0 (le bot teste `is None`, pas la
  faussete, cf. `storage._is_cleared`).
- `custom_fields` est un TABLEAU JSONB d'objets `{"label": ..., "value": ...}`,
  volontairement heterogene (c'est du texte utilisateur libre). Le CHECK ne
  garde que la FORME EXTERNE: type tableau et au plus 5 entrees. Les bornes
  internes (label 30, value 100) vivent en Python.
- `gaming_ids` est un OBJET JSONB indexe par la whitelist du registre
  (`switch`, `3ds`, `battletag`, `riot`, `steam_id`). Le CHECK ne garde que
  "c'est un objet". Une cle inconnue ou une valeur trop longue PASSE le CHECK.
- Aucune des deux colonnes JSONB n'est jamais NULL: leur "vide" est `[]` et
  `{}`.

Ce que le bot fait a la LECTURE, et qui vous protege sans vous excuser:
`storage._row_to_profile` re-valide les deux colonnes JSONB entree par entree a
travers le registre et JETTE ce qui ne passe plus (une paire trop longue, une
cle de gamer ID inconnue, un type absurde), au lieu de faire confiance a la
ligne. Une ligne hostile ne casse donc pas `/profile view` de son proprietaire -
mais les entrees fautives ne s'affichent simplement jamais, et personne ne vous
le dira. La validation doit avoir lieu a l'ECRITURE, cote Node.

### Requetes canoniques (copiez-les)

Chaque colonne s'ecrit INDEPENDAMMENT, jamais un "set both or fail":

```sql
-- Poser une valeur (bio / pronouns / accent / une colonne JSONB entiere)
INSERT INTO user_profiles (user_id, bio)
VALUES ($1, $2)
ON CONFLICT (user_id) DO UPDATE SET bio = EXCLUDED.bio, updated_at = now();
```

```sql
-- EFFACER un champ: UPDATE, jamais un upsert.
UPDATE user_profiles SET bio = NULL, updated_at = now() WHERE user_id = $1;
```

La distinction n'est pas cosmetique. Vider un champ ne doit pas faire naitre une
ligne toute-NULL pour quelqu'un qui n'a pas de profil: `/mydata` rendrait alors
un objet "profile" fait de nulls a une personne qui n'a jamais rien ecrit, et
votre propre dashboard compterait cette personne comme ayant un profil. C'est
exactement la discipline de `storage.set_field`.

Pour UN gamer ID, fusionnez cote serveur, ne faites pas de read-modify-write
(deux onglets ouverts ne doivent pas s'ecraser):

```sql
-- Poser un gamer ID
INSERT INTO user_profiles (user_id, gaming_ids)
VALUES ($1, jsonb_build_object($2::text, $3::text))
ON CONFLICT (user_id) DO UPDATE SET
    gaming_ids = user_profiles.gaming_ids
                 || jsonb_build_object($2::text, $3::text),
    updated_at = now();

-- Effacer un gamer ID (UPDATE, meme raison que ci-dessus)
UPDATE user_profiles SET gaming_ids = gaming_ids - $2::text, updated_at = now()
WHERE user_id = $1;
```

### Le piege de la table `profiles` (legacy)

Les gamer IDs viennent d'une table pre-migration toujours vivante:

| cle `gaming_ids` | colonne `profiles` |
|---|---|
| `switch` | `switch_fc` |
| `3ds` | `threeds_fc` |
| `battletag` | `battletag` |
| `riot` | `riotid` |
| `steam_id` | `steamid` |

`storage.set_gaming_id` NULLe la colonne legacy correspondante DANS LA MEME
TRANSACTION que l'ecriture du nouveau. Raison: `profiles` est encore exportee
par `/mydata` (`tools/privacy.py`, cle `legacy_profile`), donc laisser la copie
en place rendrait a l'utilisateur une valeur qu'il croit avoir remplacee ou
effacee. Le dashboard DOIT faire pareil, dans une transaction:

```sql
UPDATE profiles SET steamid = NULL WHERE user_id = $1 AND steamid IS NOT NULL;
```

Bonne nouvelle sur un point voisin: le fixup de boot
`user_profiles_import_legacy_gaming_ids` (`tools/fixups.py`) est one-shot,
enregistre dans `applied_fixups`, et sa clause de conflit met l'objet STOCKE a
droite (`EXCLUDED.gaming_ids || user_profiles.gaming_ids`), donc les cles
existantes gagnent. Un redemarrage ne peut pas ressusciter un gamer ID que vous
venez d'effacer.

`updated_at = now()` sur CHAQUE write, y compris un effacement partiel. C'est la
seule piste d'audit de la table, et le bot la maintient de la meme facon a
chaque appel de son seam.

## Table `profile_visibility`

```sql
CREATE TABLE profile_visibility (
    user_id BIGINT NOT NULL,
    field   TEXT   NOT NULL,
    level   TEXT   NOT NULL CHECK (level IN ('public', 'server', 'private')),
    PRIMARY KEY (user_id, field)
);
```

- Cle primaire composite `(user_id, field)`: une ligne par (personne, section).
- `field` est du TEXT libre, valide contre le registre Python et non contre une
  enum SQL - c'est ce qui rend un futur connecteur adressable sans migration. Un
  nom que le code tournant ne connait pas est IGNORE a la lecture, jamais
  affiche (`visibility.level_for` + `resolve_visible_fields`).
- Les trois niveaux, du plus ouvert au plus ferme: `public` (n'importe qui, y
  compris un lecteur qui ne partage aucun serveur), `server` (les membres d'un
  serveur que le proprietaire partage aussi), `private` (le proprietaire seul).

### LA REGLE CARDINALE: une ligne absente = private

Le defaut n'est JAMAIS materialise. "L'utilisateur n'a jamais decide" et
"l'utilisateur a choisi private" sont un seul et meme etat, et les deux echouent
en fermeture. Un profil nait donc entierement invisible, et seul ce que son
proprietaire a explicitement allume en sort.

Concretement, pour le dashboard:

```sql
-- public / server: upsert
INSERT INTO profile_visibility (user_id, field, level)
VALUES ($1, $2, $3)
ON CONFLICT (user_id, field) DO UPDATE SET level = EXCLUDED.level;

-- private: DELETE. N'ecrivez JAMAIS la chaine 'private'.
DELETE FROM profile_visibility WHERE user_id = $1 AND field = $2;
```

Le CHECK accepte `'private'`, donc Postgres ne vous arretera pas: c'est
precisement pourquoi c'est ecrit ici. Une ligne `'private'` materialisee n'est
pas une faute d'affichage (`level_for` la lit et rend bien private), c'est une
faute de MODELE: elle transforme une absence en donnee, elle fait diverger deux
representations du meme etat, et elle casse l'idempotence du seed de migration
`profile_visibility_seed_legacy_gaming_ids`, dont la garde `NOT EXISTS` a ete
ecrite exactement pour que "l'utilisateur a repasse en private (= a supprime la
ligne)" ne puisse pas etre re-publie par un replay. Le bot n'a qu'une
implementation de cette regle (`storage.set_visibility`, ou `PRIVATE` fait un
DELETE); le dashboard doit en etre la copie exacte.

Corollaire pour l'UI: ne rendez pas "private" a partir de la presence d'une
ligne. Rendez "private" quand il n'y a pas de ligne, et rien d'autre.

## Table `profile_connections`

```sql
CREATE TABLE profile_connections (
    user_id      BIGINT      NOT NULL,
    connector    TEXT        NOT NULL,
    external_id  TEXT        NOT NULL,
    display_name TEXT,
    linked_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_refresh TIMESTAMPTZ,
    payload      JSONB       NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (user_id, connector),
    CONSTRAINT profile_connections_connector_known
        CHECK (connector IN ('anilist', 'steam', 'lastfm', 'osu', 'backloggd',
                             'presence_gaming', 'spotify_presence')),
    CONSTRAINT profile_connections_external_id_length
        CHECK (char_length(external_id) BETWEEN 1 AND 190),
    CONSTRAINT profile_connections_display_name_length
        CHECK (display_name IS NULL OR char_length(display_name) <= 190),
    CONSTRAINT profile_connections_payload_shape
        CHECK (jsonb_typeof(payload) = 'object'),
    CONSTRAINT profile_connections_payload_size
        CHECK (octet_length(payload::text) <= 8192)
);
CREATE INDEX profile_connections_refresh_idx
    ON profile_connections (connector, last_refresh NULLS FIRST);
```

- Cle primaire `(user_id, connector)`: au plus UNE ligne par personne et par
  connecteur, donc au plus SEPT lignes par personne, par construction.
- `connector` est un CHECK a sept valeurs, PAS du TEXT libre comme
  `profile_visibility.field`: une ligne ici coute du stockage et un budget de
  refresh, donc un nom inconnu doit echouer bruyamment.
- AUCUN SECRET NE VIT ICI. AniList garde son propre `anilist_tokens` chiffre
  (Fernet); les autres connecteurs v1 sont indexes par un identifiant PUBLIC
  (un pseudo, un SteamID64). `payload` est exporte VERBATIM par `/mydata`: si un
  jour un token doit exister, il va dans sa propre table chiffree, jamais ici.
- `payload` est un CACHE d'affichage borne (des compteurs, quelques titres, une
  URL d'avatar), jamais la source de verite. Cap 8 KiB.

### La mise en garde numerique sur `payload`

`profile_connections_payload_size` mesure `octet_length(payload::text)`, c'est a
dire la forme textuelle CANONIQUE que jsonb re-serialise, PAS les octets que
vous avez envoyes. Pour les chaines, entiers, booleens, nulls et l'imbrication,
les deux comptes sont identiques octet pour octet (les separateurs par defaut de
`json.dumps`, `', '` et `': '`, sont exactement ce que jsonb emet) et un
reordonnancement de cles ne change pas la longueur. Mais les NOMBRES sont
canonises: un flottant ecrit en notation exponentielle se re-developpe, `1e+50`
devient ses 51 chiffres, et un payload que vous auriez mesure a 8163 octets est
alors REFUSE. Sonde contre l'instance locale: 8192 octets exactement passent,
8193 leve `CheckViolationError`.

Regle pratique: gardez de la marge sous 8 KiB, ou stockez les valeurs numeriques
tierces comme des chaines. Cote bot, `base.MAX_SANE_NUMBER` (10^12) existe pour
la meme raison. Ceci dit, voir plus bas: le dashboard n'ecrit JAMAIS de payload.

### Les lignes marker de presence

`presence_gaming` et `spotify_presence` sont dans le CHECK, mais ce ne sont pas
des comptes "liables" par un handle (`base.PRESENCE_SECTIONS` vs
`base.LINKABLE`). Il n'y a rien a taper: LA LIGNE EST LE CONSENTEMENT. Son
`external_id` vaut `str(user_id)` - l'identifiant Discord du proprietaire
lui-meme, seul identifiant dont ce type de section dispose (cf.
`connectors/storage.set_marker`).

Deux consequences que vous verrez dans les donnees:

- `spotify_presence` ne persiste RIEN: la section est rendue depuis
  `member.activities` (le cache gateway) au moment de la vue. Sa ligne n'a donc
  jamais de payload utile.
- `presence_gaming` porte un agregat cumulatif de minutes par jeu, rempli par le
  collecteur toutes les `FLUSH_INTERVAL = 300` secondes. C'est l'historique de
  quelqu'un: n'y touchez pas.

## Ce que le dashboard PEUT ecrire

1. **Le profil socle** - `user_profiles`: `bio`, `pronouns`, `accent`,
   `custom_fields`, `gaming_ids` (avec le nettoyage de la colonne legacy).
2. **Les visibilites** - `profile_visibility`, upsert pour public/server et
   DELETE pour private, pour les 12 noms du registre (les 5 champs stockes ET
   les 7 sections connecteur: publier une section connecteur avant qu'elle
   existe est legitime, la carte ne dessine une section que si sa ligne de
   connexion existe vraiment).
3. **L'unlink** - supprimer une ligne `profile_connections`, ce qui inclut
   l'opt-out d'une section presence. Mais faites-le comme le bot: DELETE de la
   ligne ET DELETE de la ligne de visibilite de cette section, DANS UNE SEULE
   TRANSACTION.

```sql
BEGIN;
DELETE FROM profile_connections WHERE user_id = $1 AND connector = $2;
DELETE FROM profile_visibility  WHERE user_id = $1 AND field     = $2;
COMMIT;
```

Une section publiee mais vide n'est pas neutre: c'est une promesse que la carte
ne peut pas tenir, et surtout, un re-link ulterieur re-exposerait des donnees au
niveau choisi il y a des mois pour un AUTRE compte. C'est exactement ce que fait
`connectors/storage.unlink`, en une transaction, en passant par le seam parent
pour que "private = pas de ligne" n'ait qu'une implementation.

4. **L'effacement total** - les quatre DELETE de
   `tools/privacy.PROFILE_DELETE_QUERIES` (`user_profiles`,
   `profile_visibility`, `profile_connections`, `profiles`) dans UNE
   transaction. Voir la reserve sur le collecteur presence plus bas.

## Ce que le dashboard ne doit JAMAIS faire

### 1. Creer une ligne marker de presence

`INSERT INTO profile_connections (user_id, 'presence_gaming', ...)` depuis le
dashboard est interdit, et pas seulement par politesse de consentement.

Deux moities de l'opt-in ne peuvent pas se faire hors du process bot:

- **Le desarmement du collecteur.** Le listener chaud
  `ProfilePresence.on_presence_update` teste `after.id not in self._opted` en
  PREMIERE instruction et retourne: pas d'await, pas d'allocation, pas de
  requete. `self._opted` est un `set` en memoire, rempli UNE fois au `cog_load`
  par `storage.get_opted_users`. Une ligne marker creee par le dashboard n'entre
  donc dans ce set qu'au prochain REDEMARRAGE du bot: entre-temps, l'utilisateur
  croit avoir active la collecte et rien n'est collecte.
- **Le seed du cache membre.** Le bot tourne avec
  `chunk_guilds_at_startup=False`; un membre jamais vu n'est pas en cache et
  `parse_presence_update` jette ses evenements. `_seed_member_cache` emet une
  requete gateway `query_members` ciblee sur exactement la personne qui vient de
  dire "on". C'est une operation GATEWAY: le dashboard ne peut pas l'emettre.

Le consentement presence se donne donc uniquement in-Discord, via
`/profile presence`. Le dashboard peut l'AFFICHER et l'ETEINDRE (voir l'unlink
ci-dessus), pas l'allumer.

### 2. Ecrire un `payload` de connecteur

Le bot possede le refresh. `connectors/storage.set_payload` est un UPDATE et
JAMAIS un upsert, exactement pour qu'un refresh qui atterrit apres un unlink
n'ecrive rien (un upsert ressusciterait la ligne, et le handle, que l'utilisateur
vient de supprimer). Il pose `last_refresh = now()`, et le planificateur du cog
(`_schedule_stale_refreshes`, TTL par connecteur, plancher
`CONNECTOR_REFRESH_MIN_INTERVAL = 300` s, backoff sur echec, plafond de 8
refresh en vol) decide quand rafraichir. Un payload ecrit par le dashboard sera
ecrase au prochain refresh, ou pire, faussera `last_refresh` et retardera un
vrai refresh. Pour `presence_gaming`, ce serait carrement de la corruption
d'historique: le flush fusionne (`merge_games`) sur la base de ce qu'il vient de
lire.

Meme regle pour `linked_at` et `last_refresh`: ce sont des horodatages que le
bot pose.

### 3. Toucher `anilist_tokens`

```sql
CREATE TABLE anilist_tokens (
    user_id BIGINT      PRIMARY KEY,
    token   TEXT        NOT NULL,
    expires TIMESTAMPTZ
);
```

`token` est un ciphertext Fernet dont la CLE vit dans la config du bot, jamais
en base. Le dashboard ne peut ni le lire utilement, ni en produire un valide, et
n'a aucune raison d'en supprimer un: c'est le linking de compte AniList du cog
AniList, une feature separee que le connecteur de profil `anilist` (qui, lui,
n'utilise qu'un pseudo PUBLIC) ne touche pas. Aucune lecture, aucune ecriture,
aucun DELETE.

## L'invariant d'effacement des connexions

Si le dashboard supprime des lignes `profile_connections`, que fait le bot?

**Pour les cinq connecteurs a handle** (`anilist`, `steam`, `lastfm`, `osu`,
`backloggd`): rien a reconcilier. Il n'existe aucun cache memoire de ces lignes;
`/profile view` les relit a chaque affichage. La suppression est effective
IMMEDIATEMENT. Un refresh deja en vol ne peut rien ressusciter, parce que
`set_payload` est un UPDATE et leve `NotLinked` sur zero ligne affectee.

**Pour `spotify_presence`**: idem, immediat. Cette section n'a aucun etat
memoire cote collecteur (seul `presence_gaming` alimente `_opted`, cf.
`_turn_on` et `cog_load`).

**Pour `presence_gaming`**: la ligne est le consentement, et le flush la
re-verifie. A chaque tick (`FLUSH_INTERVAL = 300` s), `flush()` fait un
`get_payloads` groupe sur les utilisateurs qui ont accumule des minutes pendant
l'intervalle; un utilisateur dont la lecture ne trouve plus de ligne est traite
comme ayant retire son consentement: ses minutes sont JETEES (pas gardees pour
plus tard) et `forget_user` le retire de `_opted` et des sessions en cours, donc
son evenement suivant est rejete par le test O(1). Meme si cette lecture le
manquait, `set_payload` leverait `NotLinked` un aller-retour plus tard, avec le
meme traitement.

Ce qui est donc GARANTI apres votre DELETE: aucune donnee ne peut plus etre
ecrite pour cette personne par le collecteur ni par un refresh, quel que soit le
moment. Tous ces chemins passent par `set_payload`, un UPDATE sur une ligne qui
n'existe plus. Seul un nouvel opt-in explicite in-Discord (`set_marker`, qui est
un INSERT) peut recreer la ligne, ce qui est le comportement voulu.

Ce qui est BORNE mais pas instantane: le desarmement en memoire. La reconciliation
se produit au flush qui suit une accumulation de minutes, soit une fenetre
<= 300 s A PARTIR DU MOMENT OU la personne joue. Si elle ne joue pas, rien n'est
accumule, il n'y a rien a purger, et l'entree restee dans `_opted` est inerte:
elle sera reconciliee au premier flush qui suit sa prochaine partie. La seule
chose qui vit dans cette fenetre est un compteur de minutes en memoire, qui sera
jete.

**Il n'existe AUJOURD'HUI aucun `kind` de notification permettant un effacement
IMMEDIAT cote collecteur.** Le seam existe cote bot
(`presence.forget_collected_presence`, appele par `profile clear` et par
`/mydata deleteprofile`), mais il n'est joignable que dans le process. Deux
options, dans cet ordre de preference:

1. **Recommande**: faire passer les effacements presence par Discord, soit
   `/profile presence gaming off`, soit `/mydata deleteprofile`. Les deux
   desarment le collecteur sur-le-champ. Le dashboard peut parfaitement pointer
   l'utilisateur vers la commande plutot que d'ecrire lui-meme.
2. **Acceptable**: supprimer la ligne depuis le dashboard et assumer la fenetre
   documentee ci-dessus, qui ne laisse fuiter aucune ecriture. Documentez-la
   dans votre UI si vous affichez "efface" a l'utilisateur.

Si un jour l'immediatete devient requise, ce sera un nouveau `kind` de
`pg_notify` - et il faudra d'abord elargir le payload, qui est aujourd'hui
guild-scoped (voir la section suivante).

## Notifications: aucun `kind` profil aujourd'hui, et c'est correct

`cogs/system/dashboard_sync.py` definit `VALID_KINDS`:

```
prefix, autorole, modlog, muterole, welcome, starboard, automod, leveling,
rank_card, warn_escalation, verify_role, locale, custom_commands, twitch,
autorooms
```

Aucun `kind` de profil. Et le format du payload ne pourrait pas en porter un tel
quel: `_parse_payload` exige un `guildId` parsable en `int` et rejette la
notification sinon, alors qu'un profil n'a pas de `guild_id` du tout.

**Aucun `pg_notify` n'est necessaire apres une ecriture de profil ou de
visibilite.** La raison, verifiee et non supposee: il n'existe aucun cache DB
partage cote bot pour ces trois tables. Chaque surface relit.

- `Profiles.profile_view` (`cogs/community/profile/cog.py`) fait TROIS lectures a
  chaque affichage de carte: `storage.get_profile`, `storage.get_visibility`,
  puis `connectors_storage.get_connections`. Aucune n'est memoisee, il n'existe
  aucun dictionnaire d'instance qui les conserve.
- `/profile panel` relit `get_visibility` a l'ouverture, et
  `ProfileVisibilityPanel.set_level` RE-LIT la carte de visibilite complete
  apres chaque clic - le commentaire du code cite nommement le dashboard comme
  l'un des ecrivains possibles entre deux clics.
- Les seuls etats memoire du package sont: `Cooldowns` / `_ConnectorFailures`
  (des limiteurs de cadence de refresh, pas des donnees), et le `_opted` de la
  presence (traite ci-dessus, et hors de votre portee de toute facon).

Donc: ecrivez, et le prochain `/profile view` voit deja votre ecriture. C'est le
contraire du contrat `rank_cards`, ou la notification etait obligatoire parce que
`Leveling._rank_cards` sert un cache sans TTL.

## La validation a reproduire cote Node

Les CHECK SQL ne sont que la ceinture; les bretelles sont en Python. Ce qui suit
est `registry.py` transcrit, et doit etre implemente cote dashboard AVANT tout
INSERT.

**Bornes des champs stockes**

| Champ | Regle | Constante |
|---|---|---|
| `bio` | texte, trim, <= 300 caracteres | `BIO_MAX = 300` |
| `pronouns` | texte, trim, <= 40 caracteres | `PRONOUNS_MAX = 40` |
| `accent` | int 0..0xFFFFFF | `rank_card.ACCENT_MAX` |
| `custom_fields` | au plus 5 paires | `CUSTOM_FIELDS_MAX = 5` |
| `custom_fields[].label` | texte, trim, <= 30 | `CUSTOM_LABEL_MAX = 30` |
| `custom_fields[].value` | texte, trim, <= 100 | `CUSTOM_VALUE_MAX = 100` |
| `gaming_ids[<cle>]` | texte, trim, <= 1000 | `GAMING_ID_MAX = 1000` |

Details qui comptent:

- **Trim puis "vide = efface"**: une valeur qui se reduit a la chaine vide apres
  strip n'est pas stockee comme `""`, elle EFFACE le champ (`_clean_text`
  renvoie `None`). Un `""` en base est une valeur que le bot n'ecrit jamais.
- **Non-chaine = refus**: un nombre ou un booleen la ou un texte est attendu est
  refuse (`InvalidValue(reason='type')`), pas coerce.
- **Paire a moitie remplie = ignoree**: dans `custom_fields`, une entree dont le
  label OU la valeur est vide est SUPPRIMEE de la liste, pas stockee comme ligne
  blanche. Le decompte des 5 se fait APRES ce filtrage.
- **`gaming_ids` est une whitelist stricte**: les cles autorisees sont
  exactement `switch`, `3ds`, `battletag`, `riot`, `steam_id`, dans cet ordre
  d'affichage. Une cle hors liste fait echouer TOUT l'objet
  (`InvalidValue(reason='unknown_key')`), elle n'est pas ignoree. Une cle dont la
  valeur est vide apres trim n'est simplement pas stockee.
- **`steam_id` n'est PAS `steam`**: `steam_id` est la cle d'un code ami tape a la
  main dans `gaming_ids`; `steam` est la SECTION du compte lie
  (`profile_connections`). Deux donnees differentes, deux visibilites
  differentes, deux noms differents. Un test du repo interdit qu'une cle de
  gamer ID egale un nom de champ du registre.
- **`accent` accepte plusieurs formes en entree** (`rank_card.validate_accent`,
  partage avec la carte de rang): un `int`, ou une chaine hex `#5865F2`,
  `5865F2`, `0x5865F2`. Le raccourci a 3 chiffres S'ETEND comme en CSS (`#FFF`
  est blanc, 0xFFFFFF, pas 0x000FFF). Toute autre longueur est REFUSEE plutot
  que completee par des zeros: `#12345` est une faute de frappe. Un booleen est
  refuse explicitement (`True` deviendrait 0x000001). Ce qui est STOCKE est
  toujours l'entier.

**Niveaux de visibilite**: exactement `public`, `server`, `private`, en
minuscules apres trim (`visibility.normalise_level`). Tout le reste leve
`InvalidLevel`. Rappel: `private` = DELETE.

**Sections valides**: les 12 noms du registre, ni plus ni moins (tableau
ci-dessous). Pour `profile_visibility.field`, un nom hors liste passera le
schema (c'est du TEXT) mais sera ignore par le bot a la lecture: vous auriez
ecrit une ligne morte. Pour `profile_connections.connector`, un nom hors des sept
est refuse par le CHECK.

## Les 12 sections du registre, et qui les ecrit

Ordre d'affichage = ordre de `registry.FIELDS`.

| # | Nom | Type | Stockage | Ecrit par le bot | Ecrit par le dashboard |
|---|---|---|---|---|---|
| 1 | `bio` | texte | `user_profiles.bio` | `/profile set bio` | OUI |
| 2 | `pronouns` | texte | `user_profiles.pronouns` | `/profile set pronouns` | OUI |
| 3 | `accent` | couleur | `user_profiles.accent` | `/profile set accent` | OUI |
| 4 | `custom_fields` | paires | `user_profiles.custom_fields` | AUCUNE commande aujourd'hui | OUI - vous etes le premier ecrivain |
| 5 | `gaming_ids` | mapping | `user_profiles.gaming_ids` | `/profile set switch\|3ds\|battletag\|riot\|steam_id`, `/profile edit` | OUI (+ nettoyage `profiles`) |
| 6 | `anilist` | connecteur | `profile_connections` | `/connections link anilist` + refresh | unlink SEULEMENT |
| 7 | `steam` | connecteur | `profile_connections` | `/connections link steam` + refresh | unlink SEULEMENT |
| 8 | `lastfm` | connecteur | `profile_connections` | `/connections link lastfm` + refresh | unlink SEULEMENT |
| 9 | `osu` | connecteur | `profile_connections` | `/connections link osu` + refresh | unlink SEULEMENT |
| 10 | `backloggd` | connecteur | `profile_connections` | `/connections link backloggd` + refresh | unlink SEULEMENT |
| 11 | `presence_gaming` | presence | `profile_connections` (marker + agregat) | `/profile presence gaming on/off`, collecteur | opt-out SEULEMENT |
| 12 | `spotify_presence` | presence | `profile_connections` (marker seul) | `/profile presence spotify on/off` | opt-out SEULEMENT |

Trois precisions sur ce tableau:

- **`custom_fields` n'a AUCUN ecrivain cote bot aujourd'hui.** C'est delibere:
  `TEXT_SETTABLE = ("bio", "pronouns", "accent")` et le commentaire au-dessus dit
  qu'une liste de paires label/valeur a sa place dans un panneau, pas dans un
  argument de chat. `/profile edit` est un formulaire de GAMER ID uniquement (un
  RadioGroup sur `GAMING_ID_KEYS` plus un champ texte). La colonne existe, le
  registre la valide, la carte la dessine, et le bot ne l'ecrit jamais - le
  dashboard sera donc son PREMIER et seul ecrivain. Raison de plus pour que la
  validation Node soit exacte: aucune commande in-Discord ne pourra reparer une
  paire mal ecrite, seule sa suppression le pourra.
- Les 12 sont adressables en visibilite par le dashboard, sans exception:
  `storage.set_visibility` accepte tout nom connu du registre, y compris une
  section dont la donnee n'existe pas encore. Cote bot, la commande texte
  `/profile visibility` n'expose que les 5 champs stockes
  (`VISIBILITY_CHOICES = registry.STORED_NAMES`), tandis que le panneau graphique
  offre bien les 12. Ce n'est pas une contrainte de stockage, seulement un choix
  d'ergonomie de la commande texte.
- `/connections link` n'accepte que les 5 connecteurs a handle
  (`LINKABLE`); les deux sections presence passent par `/profile presence`.

## Ce qui se passe si vous desobeissez

| Violation | Consequence |
|---|---|
| `bio` > 300 / `pronouns` > 40 | INSERT rejete par `user_profiles_bio_length` / `..._pronouns_length` (erreur DB explicite) |
| `accent` hors 0..0xFFFFFF | INSERT rejete par `user_profiles_accent_range` (erreur DB explicite) |
| `custom_fields` > 5 entrees, ou pas un tableau | INSERT rejete par `user_profiles_custom_fields_shape` |
| `custom_fields` avec un label de 80 caracteres | PAS d'erreur DB. La paire est JETEE a chaque lecture par `storage._sanitise_custom_fields`: l'utilisateur voit son champ disparaitre de sa carte sans explication |
| `gaming_ids` avec une cle hors whitelist | PAS d'erreur DB (le CHECK ne verifie que "objet"). La cle est jetee a chaque lecture par `_sanitise_gaming_ids` |
| `gaming_ids` ecrit sans NULLer la colonne `profiles` correspondante | PAS d'erreur DB. `/mydata` continue de rendre l'ancienne valeur dans `legacy_profile`: l'utilisateur recupere un code ami qu'il croit avoir efface |
| Effacer un champ par un upsert au lieu d'un UPDATE | PAS d'erreur DB. Cree une ligne fantome toute-NULL pour quelqu'un sans profil: `/mydata` rend un "profile" de nulls et vos propres compteurs de profils mentent |
| Ecrire `level = 'private'` au lieu de DELETE | PAS d'erreur DB (le CHECK l'accepte). Etat materialise la ou le modele exige une absence: divergence des deux representations, et le seed de migration perd son idempotence |
| DELETE d'une connexion sans DELETE de sa visibilite | PAS d'erreur DB. Section publiee sans donnee derriere; au re-link, les donnees du NOUVEAU compte sont re-exposees au niveau choisi pour l'ANCIEN |
| INSERT d'un marker `presence_gaming` | PAS d'erreur DB. Rien n'est collecte jusqu'au prochain redemarrage du bot (`_opted` n'est pas recharge), et le membre peut ne jamais entrer dans le cache gateway: opt-in silencieusement mort |
| Ecriture d'un `payload` de connecteur | PAS d'erreur DB si les caps passent. Ecrase au prochain refresh, `last_refresh` fausse; sur `presence_gaming`, corruption d'un historique cumulatif |
| `payload` a 8100 octets contenant un flottant en notation exponentielle | REJETE par `profile_connections_payload_size` alors que votre compte disait "ca passe": jsonb canonise les nombres (voir la mise en garde numerique) |
| Toute ecriture dans `anilist_tokens` | Corruption d'un secret chiffre dont vous n'avez pas la cle; le cog AniList casse pour cet utilisateur |
| Pas de `pg_notify` apres une ecriture de profil | RIEN. Il n'y a aucun cache a invalider (voir "Notifications") |

## Codes d'erreur suggeres (reponse API dashboard -> UI)

Alignes sur les exceptions typees du bot, pour que le message du dashboard et
celui de `/profile set` racontent la meme histoire face a la meme saisie.

| Code | Cause | Miroir bot |
|---|---|---|
| `PROFILE_UNKNOWN_FIELD` | Nom hors du registre | `registry.UnknownField` |
| `PROFILE_FIELD_NOT_STORED` | Champ reel mais non stockable (une section connecteur) | `registry.FieldNotStored` |
| `PROFILE_INVALID_TYPE` | Type errone (nombre la ou du texte est attendu) | `registry.InvalidValue(reason='type')` |
| `PROFILE_TOO_LONG` | Depasse `BIO_MAX` / `PRONOUNS_MAX` / `CUSTOM_*_MAX` / `GAMING_ID_MAX` | `registry.InvalidValue(reason='too_long', limit=...)` |
| `PROFILE_TOO_MANY` | Plus de 5 paires dans `custom_fields` | `registry.InvalidValue(reason='too_many', limit=5)` |
| `PROFILE_UNKNOWN_GAMING_KEY` | Cle hors de `GAMING_ID_KEYS` | `registry.InvalidValue(reason='unknown_key')` |
| `PROFILE_INVALID_COLOUR` | Hex mal forme ou hors 0..0xFFFFFF | `registry.InvalidValue(reason='colour')` / `rank_card.InvalidAccent` |
| `PROFILE_INVALID_LEVEL` | Niveau hors `public`/`server`/`private` | `visibility.InvalidLevel` |
| `PROFILE_UNKNOWN_CONNECTOR` | Nom hors des sept sections | `connectors.base.UnknownConnector` |
| `PROFILE_NOT_LINKED` | Unlink d'une connexion inexistante | `connectors.base.NotLinked` |

`InvalidValue` porte `name`, `reason` et `limit`: reproduisez les trois, c'est ce
qui permet a l'UI de dire "300 caracteres maximum" au lieu de "invalide".

## References cote bot (pour le debug croise)

- `cogs/community/profile/registry.py` - les 12 champs, les caps, la whitelist
  de gamer IDs, les erreurs typees.
- `cogs/community/profile/visibility.py` - les trois niveaux, `level_for`,
  `can_view`, `resolve_visible_fields`, et la regle "absent = private".
- `cogs/community/profile/storage.py` - les requetes canoniques de
  `user_profiles` et `profile_visibility`, la re-validation JSONB en lecture, le
  nettoyage de la table legacy.
- `cogs/community/profile/connectors/base.py` - `SECTIONS`, `LINKABLE`,
  `PRESENCE_SECTIONS`, les caps (`EXTERNAL_ID_MAX`, `DISPLAY_NAME_MAX`,
  `PAYLOAD_MAX_BYTES`, `URL_MAX`, `MAX_SANE_NUMBER`).
- `cogs/community/profile/connectors/storage.py` - `link`, `set_marker`,
  `unlink` (la transaction a deux DELETE), `set_payload` (UPDATE, jamais upsert).
- `cogs/community/profile/presence.py` - `_opted`, `FLUSH_INTERVAL`, `flush` et
  sa reconciliation, `forget_collected_presence`, `_seed_member_cache`.
- `cogs/community/profile/cog.py` - `profile_view` (les trois lectures, sans
  cache), le planificateur de refresh des connecteurs.
- `cogs/system/dashboard_sync.py` - `VALID_KINDS` et `_parse_payload` (le
  `guildId` obligatoire).
- `tools/privacy.py` - `PROFILE_DELETE_QUERIES`, l'export `/mydata` (v3).
- `tools/fixups.py` - les deux fixups de migration du profil et leur ordre.
- `schema.sql` - la definition canonique des trois tables et de tous les CHECK
  cites ici.
