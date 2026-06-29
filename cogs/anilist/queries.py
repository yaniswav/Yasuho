MEDIA_QUERY = """
query ($id: Int, $search: String, $type: MediaType) {
  Media(id: $id, search: $search, type: $type) {
    id
    idMal
    type
    title { romaji english native }
    format
    status
    episodes
    chapters
    duration
    averageScore
    meanScore
    popularity
    favourites
    genres
    siteUrl
    bannerImage
    coverImage { large color }
    description(asHtml: false)
    season
    seasonYear
    studios(isMain: true) { nodes { name } }
    trailer { site id }
    relations {
      edges {
        relationType
        node { id title { romaji } format type }
      }
    }
    characters(sort: ROLE, perPage: 12) {
      edges { role node { name { full } } }
    }
    recommendations(sort: RATING_DESC, perPage: 10) {
      nodes { mediaRecommendation { id title { romaji } format } }
    }
    rankings { rank type context allTime }
    stats {
      scoreDistribution { score amount }
      statusDistribution { status amount }
    }
  }
}
"""

# Lightweight search used to gather a handful of candidates for the picker.
CANDIDATE_QUERY = """
query ($search: String, $type: MediaType) {
  Page(perPage: 10) {
    media(search: $search, type: $type) {
      id
      title { romaji english }
      format
      seasonYear
    }
  }
}
"""

# Cross-type search used to disambiguate list edits (anime vs manga).
SEARCH_QUERY = """
query ($search: String) {
  Page(perPage: 10) {
    media(search: $search) {
      id
      type
      format
      title { romaji english }
      episodes
      chapters
      seasonYear
    }
  }
}
"""

# Same shape as SEARCH_QUERY but tagged with the viewer's own list entry.
# ``mediaListEntry`` resolves only when the request carries the user's token,
# letting the update wizard prioritise titles already on their list.
SEARCH_ENTRY_QUERY = """
query ($search: String) {
  Page(perPage: 10) {
    media(search: $search) {
      id
      type
      format
      title { romaji english }
      episodes
      chapters
      seasonYear
      mediaListEntry { id status score progress }
    }
  }
}
"""

# Browse query for trending / popular / seasonal listings.
PAGE_QUERY = """
query ($sort: [MediaSort], $type: MediaType, $season: MediaSeason, $seasonYear: Int) {
  Page(perPage: 25) {
    media(sort: $sort, type: $type, season: $season, seasonYear: $seasonYear) {
      id
      title { romaji english }
      format
      averageScore
      episodes
      seasonYear
    }
  }
}
"""

USER_STATS_QUERY = """
query ($name: String) {
  User(name: $name) {
    name
    avatar { large }
    bannerImage
    siteUrl
    options { profileColor }
    statistics {
      anime {
        count
        meanScore
        minutesWatched
        episodesWatched
        genres(limit: 6, sort: COUNT_DESC) { genre count }
      }
      manga { count meanScore chaptersRead }
    }
    favourites {
      anime(perPage: 3) { nodes { title { romaji } } }
      manga(perPage: 3) { nodes { title { romaji } } }
      characters(perPage: 3) { nodes { name { full } } }
    }
  }
}
"""

CHARACTER_QUERY = """
query ($search: String) {
  Character(search: $search) {
    name { full native }
    image { large }
    description(asHtml: false)
    siteUrl
  }
}
"""

STUDIO_QUERY = """
query ($search: String) {
  Studio(search: $search) {
    name
    siteUrl
    media(sort: POPULARITY_DESC, perPage: 10) {
      nodes { title { romaji } }
    }
  }
}
"""

VIEWER_QUERY = """
query { Viewer { id name } }
"""

SAVE_ENTRY_QUERY = """
mutation ($mediaId: Int, $progress: Int, $status: MediaListStatus, $score: Float) {
  SaveMediaListEntry(mediaId: $mediaId, progress: $progress, status: $status, score: $score) {
    id
    status
    progress
    score
    media { title { romaji } }
  }
}
"""

MEDIA_LIST_QUERY = """
query ($userId: Int, $type: MediaType, $status: MediaListStatus) {
  MediaListCollection(userId: $userId, type: $type, status: $status) {
    lists {
      entries {
        progress
        score
        media { title { romaji } episodes chapters }
      }
    }
  }
}
"""

# Lightweight fetch by id, used to resolve an autocomplete "id:<n>" sentinel.
ID_MEDIA_QUERY = """
query ($id: Int) {
  Media(id: $id) {
    id
    type
    format
    title { romaji english }
    episodes
    chapters
    seasonYear
  }
}
"""

# Fast cross-type (anime + manga) search powering slash title autocomplete.
AUTOCOMPLETE_QUERY = """
query ($search: String) {
  Page(perPage: 12) {
    media(search: $search) {
      id
      type
      title { romaji english }
      seasonYear
    }
  }
}
"""

# The authenticated viewer's own list entry for a media. ``mediaListEntry`` is
# only resolved per-viewer when the request carries that user's OAuth token.
MEDIA_ENTRY_QUERY = """
query ($id: Int) {
  Media(id: $id) {
    mediaListEntry {
      status
      score
      progress
      progressVolumes
      repeat
      startedAt { year month day }
      completedAt { year month day }
    }
  }
}
"""
