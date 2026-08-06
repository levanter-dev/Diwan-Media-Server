from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .db import connect

OMDB_URL = "https://www.omdbapi.com/"
TMDB_URL = "https://api.themoviedb.org/3"
TMDB_POSTER_URL = "https://image.tmdb.org/t/p/w500"
TMDB_BACKDROP_URL = "https://image.tmdb.org/t/p/w780"


class ExploreError(RuntimeError):
    pass


def get_secret(name: str) -> str:
    env_value = os.environ.get(name.upper())
    if env_value:
        return env_value.strip()
    key = name.lower()
    conn = connect()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"].strip() if row else ""


def set_secret(name: str, value: str | None) -> None:
    if value is None:
        return
    clean = value.strip()
    conn = connect()
    if clean:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (name.lower(), clean),
        )
    else:
        conn.execute("DELETE FROM settings WHERE key=?", (name.lower(),))
    conn.commit()
    conn.close()


def get_setting(name: str, default: str = "") -> str:
    value = get_secret(name)
    return value if value else default


def set_setting(name: str, value: str | None) -> None:
    set_secret(name, value)


def selected_search_provider() -> str:
    provider = get_setting("explore_search_provider", "tmdb").lower()
    return provider if provider in {"tmdb", "omdb"} else "tmdb"


def provider_status() -> dict[str, Any]:
    return {
        "omdb_configured": bool(get_secret("omdb_api_key")),
        "tmdb_configured": bool(get_secret("tmdb_token")),
        "search_provider": selected_search_provider(),
    }


def _load_json(url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    request = Request(url, headers=headers or {"User-Agent": "LocalMediaServer/0.3"})
    try:
        with urlopen(request, timeout=12) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise ExploreError(f"Provider returned HTTP {exc.code}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ExploreError("Could not reach the catalogue provider") from exc


def search_omdb(query: str, media_type: str | None = None, page: int = 1) -> dict[str, Any]:
    api_key = get_secret("omdb_api_key")
    if not api_key:
        raise ExploreError("OMDb API key is not configured")
    params = {"apikey": api_key, "s": query, "page": max(1, min(page, 100))}
    if media_type in {"movie", "series", "episode"}:
        params["type"] = media_type
    data = _load_json(OMDB_URL + "?" + urlencode(params))
    if data.get("Response") == "False":
        return {"provider": "omdb", "results": [], "total": 0, "message": data.get("Error", "No results")}
    return {
        "provider": "omdb",
        "results": [_normalize_omdb(item) for item in data.get("Search", [])],
        "total": int(data.get("totalResults", 0)),
        "message": None,
    }


def search_tmdb(query: str, media_type: str | None = None, page: int = 1) -> dict[str, Any]:
    if not get_secret("tmdb_token"):
        raise ExploreError("TMDB token is not configured")
    endpoint = "multi"
    if media_type == "movie":
        endpoint = "movie"
    elif media_type == "series":
        endpoint = "tv"
    data = _tmdb_get(f"/search/{endpoint}", {"query": query, "page": page})
    results = [_normalize_tmdb(item) for item in data.get("results", [])]
    results = [item for item in results if item]
    return {"provider": "tmdb", "results": results, "total": data.get("total_results", 0), "message": None}


def explore_search(query: str, media_type: str | None = None, page: int = 1) -> dict[str, Any]:
    if selected_search_provider() == "omdb":
        return search_omdb(query, media_type, page)
    return search_tmdb(query, media_type, page)


_KNOWN_GENRES = {
    "28": "Action", "12": "Adventure", "16": "Animation", "35": "Comedy",
    "80": "Crime", "99": "Documentary", "18": "Drama", "10751": "Family",
    "14": "Fantasy", "36": "History", "27": "Horror", "10402": "Music",
    "9648": "Mystery", "10749": "Romance", "878": "Science Fiction",
    "10770": "TV Movie", "53": "Thriller", "10752": "War", "37": "Western",
    "10759": "Action & Adventure", "10762": "Kids", "10763": "News",
    "10764": "Reality", "10765": "Sci-Fi & Fantasy", "10766": "Soap",
    "10767": "Talk", "10768": "War & Politics",
}

_ADULT_EXCLUDE_KEYWORDS = "279|1089|310|2340|1920|1919|1233|2682|2188|328|1949|864|448|10164|1442|1454|1087|2219|383|9663"
_BLOOD_EXCLUDE_KEYWORDS = "1557|1848|771|2732|859|1949|1938"
_VULGAR_EXCLUDE_KEYWORDS = "16|376|1599|1625|2290|3568|3030"


def discover(media_type: str = "movie", genres: str | None = None, year_min: int | None = None,
             year_max: int | None = None, rating_min: float | None = None,
             sort_by: str = "popularity.desc", parental: str | None = None,
             page: int = 1) -> dict[str, Any]:
    if not get_secret("tmdb_token"):
        return {"provider": "tmdb", "configured": False, "results": [], "message": "TMDB token not configured"}
    params: dict[str, Any] = {"language": "en-US", "page": page, "sort_by": sort_by, "include_adult": "false"}
    if genres:
        genre_ids = [g.strip() for g in genres.split(",") if g.strip().isdigit()]
        if genre_ids:
            params["with_genres"] = "|".join(genre_ids)
    if year_min:
        params["primary_release_date.gte" if media_type == "movie" else "first_air_date.gte"] = f"{year_min}-01-01"
    if year_max:
        params["primary_release_date.lte" if media_type == "movie" else "first_air_date.lte"] = f"{year_max}-12-31"
    if rating_min:
        params["vote_average.gte"] = str(rating_min)
        params["vote_count.gte"] = "50"
    if parental:
        exclude_kw = []
        if "blood" in parental:
            exclude_kw.extend(_BLOOD_EXCLUDE_KEYWORDS.split("|"))
        if "vulgar" in parental:
            exclude_kw.extend(_VULGAR_EXCLUDE_KEYWORDS.split("|"))
        if "nudity" in parental:
            exclude_kw.extend(_ADULT_EXCLUDE_KEYWORDS.split("|"))
        if exclude_kw:
            params["without_keywords"] = "|".join(dict.fromkeys(exclude_kw))
        if "family" in parental:
            params["certification_country"] = "US"
            params["certification.lte"] = "PG-13"
            params["without_genres"] = "27"
    endpoint = "/discover/movie" if media_type == "movie" else "/discover/tv"
    data = _tmdb_get(endpoint, params)
    results = [_normalize_tmdb(item) for item in data.get("results", [])[:20]]
    results = [r for r in results if r]
    total = data.get("total_results", 0)
    return {"provider": "tmdb", "results": results, "total": total, "page": data.get("page", page),
            "total_pages": data.get("total_pages", 1), "genres": _KNOWN_GENRES, "message": None}


def explore_home() -> dict[str, Any]:
    if not get_secret("tmdb_token"):
        return {
            "provider": "tmdb",
            "configured": False,
            "message": "Add a TMDB API Read Access Token to enable trending, popular, and now-playing rows.",
            "spotlight": None,
            "rows": [],
        }

    rows = [
        ("trending", "Trending Today", "/trending/all/day", {}),
        ("popular_movies", "Popular Movies", "/movie/popular", {}),
        ("now_playing", "Now Playing Movies", "/movie/now_playing", {}),
        ("top_movies", "Top Rated Movies", "/movie/top_rated", {}),
        ("popular_series", "Popular Series", "/tv/popular", {}),
        ("airing_today", "Airing Today", "/tv/airing_today", {}),
        ("top_series", "Top Rated Series", "/tv/top_rated", {}),
    ]
    output: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        future_map = {pool.submit(_tmdb_get, path, params): (key, title) for key, title, path, params in rows}
        for future in as_completed(future_map):
            key, title = future_map[future]
            data = future.result()
            items = [_normalize_tmdb(item) for item in data.get("results", [])[:20]]
            output.append({"key": key, "title": title, "items": [item for item in items if item]})
    order = {key: index for index, (key, *_rest) in enumerate(rows)}
    output.sort(key=lambda row: order[row["key"]])
    spotlight = output[0]["items"][0] if output and output[0]["items"] else None
    return {"provider": "tmdb", "configured": True, "message": None, "spotlight": spotlight, "rows": output}


def suggestions_from_seeds(seeds: list[dict[str, Any]], media_type: str = "", genres: str = "",
                           release_date_from: str = "", release_date_to: str = "",
                           excluded_ids: set = None) -> dict[str, Any]:
    """Build filtered recommendations from explicit taste seeds."""
    if not get_secret("tmdb_token"):
        return {"provider": "tmdb", "configured": False, "results": [], "message": "Add a TMDB token to use Suggestions."}
    wanted = [media_type] if media_type in {"movie", "series"} else ["movie", "series"]
    wanted_genres = {value.strip() for value in genres.split(",") if value.strip().isdigit()}

    def matches_filters(item: dict[str, Any]) -> bool:
        item_genres = {str(value) for value in item.get("genre_ids", [])}
        if wanted_genres and wanted_genres.isdisjoint(item_genres):
            return False
        released = str(item.get("release_date") or item.get("first_air_date") or "")
        if release_date_from and (not released or released < release_date_from):
            return False
        if release_date_to and (not released or released > release_date_to):
            return False
        return True
    resolved: list[tuple[str, str, dict[str, Any]]] = []
    for seed in seeds[:12]:
        seed_type = "series" if seed.get("media_type") in {"series", "episode", "tv"} else "movie"
        external_id = str(seed.get("external_id") or "")
        if seed.get("provider") != "tmdb":
            match = search_tmdb(str(seed.get("title") or ""), seed_type).get("results", [])
            external_id = str(match[0]["id"]) if match else ""
        if not external_id:
            continue
        endpoint_type = "tv" if seed_type == "series" else "movie"
        try:
            details = _tmdb_get(f"/{endpoint_type}/{external_id}")
            resolved.append((seed_type, external_id, details))
        except ExploreError:
            continue
    if not resolved:
        return {"provider": "tmdb", "configured": True, "results": [], "message": "Select at least one recognizable movie or series."}

    excluded = {(kind, external_id) for kind, external_id, _details in resolved}
    excluded_providers = excluded_ids or set()
    ranked: dict[tuple[str, str], dict[str, Any]] = {}

    def add_candidates(items: list[dict[str, Any]], kind: str, source_weight: float) -> None:
        for position, raw in enumerate(items[:30]):
            item = dict(raw)
            if not matches_filters(item):
                continue
            item["media_type"] = "tv" if kind == "series" else "movie"
            normalized = _normalize_tmdb(item)
            if not normalized or (kind, str(normalized["id"])) in excluded:
                continue
            if normalized["media_type"] not in wanted:
                continue
            if ("tmdb", str(normalized["id"])) in excluded_providers:
                continue
            key = (kind, str(normalized["id"]))
            score = source_weight + max(0.0, 1.0 - position / 40.0) + float(normalized.get("rating") or 0) / 20.0
            if key in ranked:
                ranked[key]["_suggestion_score"] += score
            else:
                ranked[key] = {**normalized, "_suggestion_score": score}

    for seed_type, external_id, _details in resolved:
        if seed_type not in wanted:
            continue
        endpoint_type = "tv" if seed_type == "series" else "movie"
        try:
            data = _tmdb_get(f"/{endpoint_type}/{external_id}/recommendations", {"page": 1})
            add_candidates(data.get("results", []), seed_type, 3.0)
        except ExploreError:
            pass

    genre_counts: dict[str, int] = {}
    for _kind, _external_id, details in resolved:
        for genre in details.get("genres", []):
            genre_id = str(genre.get("id") or "")
            if genre_id:
                genre_counts[genre_id] = genre_counts.get(genre_id, 0) + 1
    genre_ids = [key for key, _count in sorted(genre_counts.items(), key=lambda pair: (-pair[1], pair[0]))[:5]]
    for kind in wanted:
        endpoint_type = "tv" if kind == "series" else "movie"
        params: dict[str, Any] = {"sort_by": "vote_average.desc", "vote_count.gte": 100, "page": 1, "include_adult": "false"}
        discover_genres = sorted(wanted_genres) if wanted_genres else genre_ids
        if discover_genres:
            params["with_genres"] = "|".join(discover_genres)
        if release_date_from:
            params["primary_release_date.gte" if kind == "movie" else "first_air_date.gte"] = release_date_from
        if release_date_to:
            params["primary_release_date.lte" if kind == "movie" else "first_air_date.lte"] = release_date_to
        try:
            data = _tmdb_get(f"/discover/{endpoint_type}", params)
            add_candidates(data.get("results", []), kind, 1.25)
        except ExploreError:
            pass

    results = sorted(ranked.values(), key=lambda item: (-item["_suggestion_score"], -(item.get("rating") or 0)))[:30]
    for item in results:
        item.pop("_suggestion_score", None)
    return {"provider": "tmdb", "configured": True, "results": results, "message": None, "seed_count": len(resolved)}

def _tmdb_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    token = get_secret("tmdb_token")
    if not token:
        raise ExploreError("TMDB token is not configured")
    query = {"language": "en-US", **(params or {})}
    url = TMDB_URL + path + "?" + urlencode(query)
    return _load_json(url, {"Authorization": f"Bearer {token}", "User-Agent": "LocalMediaServer/0.3"})


def _normalize_omdb(item: dict[str, Any]) -> dict[str, Any]:
    poster = item.get("Poster")
    return {
        "id": item.get("imdbID"),
        "provider": "omdb",
        "media_type": item.get("Type"),
        "title": item.get("Title"),
        "year": item.get("Year"),
        "poster_url": poster if poster and poster != "N/A" else None,
        "backdrop_url": None,
        "overview": None,
        "rating": None,
    }


def _normalize_tmdb(item: dict[str, Any]) -> dict[str, Any] | None:
    media_type = item.get("media_type") or ("series" if "first_air_date" in item else "movie")
    if media_type == "tv":
        media_type = "series"
    if media_type not in {"movie", "series"}:
        return None
    title = item.get("title") or item.get("name")
    date = item.get("release_date") or item.get("first_air_date") or ""
    poster_path = item.get("poster_path")
    backdrop_path = item.get("backdrop_path")
    return {
        "id": str(item.get("id")),
        "provider": "tmdb",
        "media_type": media_type,
        "title": title,
        "year": date[:4] if date else None,
        "poster_url": TMDB_POSTER_URL + poster_path if poster_path else None,
        "backdrop_url": TMDB_BACKDROP_URL + backdrop_path if backdrop_path else None,
        "overview": item.get("overview"),
        "rating": item.get("vote_average"),
    }


def fetch_details(provider: str, external_id: str, media_type: str | None = None) -> dict[str, Any]:
    if provider == "omdb":
        return _omdb_details(external_id)
    return _tmdb_details(external_id, media_type)


def _omdb_details(imdb_id: str) -> dict[str, Any]:
    api_key = get_secret("omdb_api_key")
    if not api_key:
        raise ExploreError("OMDb API key is not configured")
    params = {"apikey": api_key, "i": imdb_id, "plot": "full"}
    data = _load_json(OMDB_URL + "?" + urlencode(params))
    if data.get("Response") == "False":
        raise ExploreError(data.get("Error", "Title not found"))
    return {
        "provider": "omdb",
        "id": data.get("imdbID"),
        "media_type": data.get("Type"),
        "title": data.get("Title"),
        "year": data.get("Year"),
        "poster_url": _clean_poster(data.get("Poster")),
        "overview": data.get("Plot"),
        "rating": data.get("imdbRating"),
        "runtime": data.get("Runtime"),
        "genre": data.get("Genre"),
        "director": data.get("Director"),
        "writer": data.get("Writer"),
        "actors": data.get("Actors"),
        "rated": data.get("Rated"),
        "released": data.get("Released"),
        "language": data.get("Language"),
        "country": data.get("Country"),
        "awards": data.get("Awards"),
    }


def _tmdb_details(tmdb_id: str, media_type: str | None = None) -> dict[str, Any]:
    if not get_secret("tmdb_token"):
        raise ExploreError("TMDB token is not configured")
    return _tmdb_get_details(tmdb_id, media_type)


def _tmdb_get_details(tmdb_id: int | str, media_type: str | None = None) -> dict[str, Any]:
    sid = str(tmdb_id)
    requested_type = (
        "series"
        if media_type in {"series", "tv", "episode"}
        else "movie"
        if media_type == "movie"
        else None
    )
    if sid.startswith("tt"):
        search_data = _tmdb_get(f"/find/{sid}", {"external_source": "imdb_id"})
        movie_results = search_data.get("movie_results") or []
        tv_results = search_data.get("tv_results") or []
        if requested_type == "series" and tv_results:
            item = tv_results[0]
            media_type = "series"
        elif requested_type == "movie" and movie_results:
            item = movie_results[0]
            media_type = "movie"
        elif requested_type is None and movie_results:
            item = movie_results[0]
            media_type = "movie"
        elif requested_type is None and tv_results:
            item = tv_results[0]
            media_type = "series"
        else:
            suffix = f" as {requested_type}" if requested_type else ""
            raise ExploreError(f"IMDB id {sid} not found on TMDB{suffix}")
    else:
        last_error: Exception | None = None
        item = None
        candidates = [requested_type] if requested_type else ["movie", "series"]
        for candidate_type in candidates:
            endpoint = "tv" if candidate_type == "series" else "movie"
            append = (
                "content_ratings,credits,keywords,videos"
                if candidate_type == "series"
                else "release_dates,credits,keywords,videos"
            )
            try:
                item = _tmdb_get(f"/{endpoint}/{sid}", {"append_to_response": append})
                media_type = candidate_type
                break
            except Exception as exc:
                last_error = exc
        if item is None:
            suffix = f" as {requested_type}" if requested_type else ""
            raise ExploreError(f"Could not find title with id {sid}{suffix}") from last_error

    date = item.get("release_date") or item.get("first_air_date") or ""
    genres = ", ".join(g["name"] for g in item.get("genres", [])[:4])
    runtime = item.get("runtime") or (item.get("episode_run_time", [None])[0] if item.get("episode_run_time") else None)
    runtime_str = f"{runtime} min" if runtime else None

    certification = _extract_certification(item, media_type)
    content_rating_info = _extract_content_ratings(item, media_type)

    cast = []
    credits = item.get("credits", {})
    for person in credits.get("cast", [])[:8]:
        cast.append(person.get("name", ""))

    director = ""
    writers = []
    for person in credits.get("crew", []):
        if person.get("job") == "Director" and not director:
            director = person.get("name", "")
        if person.get("job") in ("Writer", "Screenplay", "Story") and person.get("name") not in writers:
            writers.append(person.get("name"))

    poster_path = item.get("poster_path")
    backdrop_path = item.get("backdrop_path")

    result: dict[str, Any] = {
        "provider": "tmdb",
        "id": str(item.get("id")),
        "media_type": media_type,
        "title": item.get("title") or item.get("name"),
        "year": date[:4] if date else None,
        "poster_url": TMDB_POSTER_URL + poster_path if poster_path else None,
        "backdrop_url": TMDB_BACKDROP_URL + backdrop_path if backdrop_path else None,
        "overview": item.get("overview"),
        "tagline": item.get("tagline"),
        "rating": item.get("vote_average"),
        "vote_count": item.get("vote_count"),
        "runtime": runtime_str,
        "genre": genres or None,
        "director": director or None,
        "writers": ", ".join(writers[:4]) if writers else None,
        "actors": ", ".join(cast) if cast else None,
        "rated": certification,
        "content_rating": content_rating_info,
        "adult": item.get("adult", False),
        "released": date,
        "status": item.get("status"),
        "language": item.get("original_language"),
        "budget": item.get("budget") or None,
        "revenue": item.get("revenue") or None,
    }
    if media_type == "series":
        result["seasons"] = item.get("number_of_seasons")
        result["episodes"] = item.get("number_of_episodes")
    if item.get("production_companies"):
        companies = [c["name"] for c in item.get("production_companies", [])[:3]]
        result["production"] = ", ".join(companies) if companies else None
    adult_keywords = _extract_adult_keywords(item)
    if adult_keywords:
        result["adult_keywords"] = adult_keywords
    videos = (item.get("videos", {}) or {}).get("results", [])
    trailer = _find_trailer(videos)
    if trailer:
        result["trailer_url"] = trailer
    return result
def _find_trailer(videos: list[dict[str, Any]]) -> str | None:
    best = None
    for v in videos:
        if v.get("site") == "YouTube" and v.get("type") == "Trailer":
            if not best or v.get("official", False):
                best = v
    if best:
        return f"https://www.youtube.com/embed/{best['key']}"
    best_teaser = None
    for v in videos:
        if v.get("site") == "YouTube" and v.get("type") in ("Teaser", "Clip"):
            if not best_teaser or v.get("official", False):
                best_teaser = v
    if best_teaser:
        return f"https://www.youtube.com/embed/{best_teaser['key']}"
    return None
def _extract_certification(item: dict[str, Any], media_type: str) -> str | None:
    if media_type == "movie":
        release_dates = item.get("release_dates", {}).get("results", [])
        for country in release_dates:
            if country.get("iso_3166_1") == "US":
                for entry in country.get("release_dates", []):
                    if entry.get("certification"):
                        return entry["certification"]
                break
    else:
        content_ratings = item.get("content_ratings", {}).get("results", [])
        for country in content_ratings:
            if country.get("iso_3166_1") == "US":
                return country.get("rating")
    return None


def _extract_content_ratings(item: dict[str, Any], media_type: str) -> list[dict[str, Any]]:
    ratings = []
    source = item.get("release_dates") if media_type == "movie" else item.get("content_ratings")
    if not source:
        return ratings
    results = source.get("results", [])
    priority = {"US": 0, "GB": 1, "DE": 2, "FR": 3, "AU": 4, "CA": 5, "BR": 6}
    for country in sorted(results, key=lambda c: priority.get(c.get("iso_3166_1", ""), 99)):
        iso = country.get("iso_3166_1", "")
        if media_type == "movie":
            for entry in country.get("release_dates", []):
                cert = entry.get("certification")
                if cert and cert.strip():
                    desc = "Movie rating" if media_type == "movie" else ""
                    ratings.append({"country": iso, "rating": cert, "description": entry.get("note") if media_type == "movie" else None})
                    break
        else:
            rating = country.get("rating")
            if rating and rating.strip():
                ratings.append({"country": iso, "rating": rating, "description": None})
    return ratings[:8]


_ADULT_KEYWORD_IDS: dict[int, str] = {
    279: "Nudity",
    1089: "Female Nudity",
    310: "Male Nudity",
    2340: "Topless",
    1920: "Female Frontal Nudity",
    1919: "Female Rear Nudity",
    1233: "Sex",
    2682: "Sex Scene",
    2188: "Erotic",
    328: "Masturbation",
    1949: "Stripper",
    864: "Lingerie",
    448: "Shower",
    10164: "Female Full Frontal Nudity",
    383: "Sexual Humor",
    9663: "Sexual Innuendo",
    2029: "Kissing",
    1454: "Teen Sex",
    1442: "Nudity (Full Frontal)",
    1087: "Breasts",
    2219: "Cleavage",
}


def _extract_adult_keywords(item: dict[str, Any]) -> list[dict[str, Any]] | None:
    keywords_data = item.get("keywords")
    if not keywords_data:
        return None
    kw_list = keywords_data.get("keywords") or keywords_data.get("results") or []
    if not kw_list:
        return None
    matched = []
    for kw in kw_list:
        kid = kw.get("id")
        if kid in _ADULT_KEYWORD_IDS:
            matched.append({"id": kid, "name": _ADULT_KEYWORD_IDS.get(kid, kw.get("name", ""))})
    return matched if matched else None


def _clean_poster(url: str | None) -> str | None:
    return url if url and url != "N/A" else None
