"""
scraping.py
===========
Extracción de contenido externo (no-Moodle) para el Automático:
- Transcripción rápida de YouTube por subtítulos/CC ya existentes (evita
  descargar el audio y correr Whisper cuando el video ya los tiene).
- Scraping de páginas web externas (texto principal del artículo).

Técnica portada de un proyecto de referencia del usuario (Scrapping, no
forma parte de este repo): youtube-transcript-api para YouTube,
requests + BeautifulSoup + trafilatura para web.
"""
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, parse_qs

import requests
import trafilatura
from bs4 import BeautifulSoup
from youtube_transcript_api import (
    YouTubeTranscriptApi,
    TranscriptsDisabled,
    NoTranscriptFound,
    VideoUnavailable,
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 15


def _extract_video_id(url: str) -> Optional[str]:
    """Extrae el video_id de distintas variantes de URL de YouTube."""
    parsed = urlparse(url)
    host = parsed.hostname or ""

    if "youtu.be" in host:
        video_id = parsed.path.lstrip("/")
        if video_id:
            return video_id

    if "youtube.com" in host:
        if parsed.path == "/watch":
            query = parse_qs(parsed.query)
            if "v" in query:
                return query["v"][0]
        match = re.match(r"^/(embed|shorts|live)/([^/?]+)", parsed.path)
        if match:
            return match.group(2)

    match = re.search(r"([a-zA-Z0-9_-]{11})", url)
    return match.group(1) if match else None


def get_youtube_transcript(url: str, languages: Optional[List[str]] = None) -> Optional[List[Dict[str, Any]]]:
    """
    Vía rápida de transcripción de YouTube: usa los subtítulos/CC (manuales
    o automáticos) que el video ya tenga, sin descargar audio ni correr
    Whisper. Retorna `[{"start": float, "end": float, "text": str}, ...]`
    (mismo shape que `strategies.base.AudioTranscriptionStrategy.transcribe`)
    o `None` si el video no tiene subtítulos disponibles en ningún idioma
    — el caller debe caer al camino existente (descarga de audio + ASR
    local) en ese caso, no es un error.
    """
    video_id = _extract_video_id(url)
    if not video_id:
        return None
    languages = languages or ["es", "en"]

    try:
        ytt_api = YouTubeTranscriptApi()
        transcript_list = ytt_api.list(video_id)

        transcript = None
        try:
            transcript = transcript_list.find_transcript(languages)
        except NoTranscriptFound:
            try:
                transcript = transcript_list.find_generated_transcript(languages)
            except NoTranscriptFound:
                transcript = next(iter(transcript_list), None)

        if transcript is None:
            return None

        fetched = transcript.fetch()
        segments = [
            {"start": snippet.start, "end": snippet.start + snippet.duration, "text": snippet.text}
            for snippet in fetched
            if snippet.text and snippet.text.strip()
        ]
        return segments or None
    except (TranscriptsDisabled, NoTranscriptFound, VideoUnavailable):
        return None
    except Exception as e:
        print(f"⚠️ No se pudo obtener transcripción de YouTube para {url}: {e}")
        return None


def scrape_web_page(url: str) -> Dict[str, Any]:
    """
    Extrae el texto principal (sin menús/boilerplate) de una página web
    externa. `trafilatura` hace el trabajo pesado (favorece precisión
    sobre recall); si no logra extraer nada, cae al <body> completo vía
    BeautifulSoup. Lanza `ValueError` si la URL no responde — el caller
    debe mostrarlo como error de validación de ese archivo, no abortar
    todo el lote.
    """
    try:
        # verify=False: mismo criterio que el resto del pipeline (descargas
        # de Moodle, API de Moodle) — algunos sitios externos citados en un
        # curso real tienen certificados mal configurados; no vale la pena
        # perder contenido legítimo por eso.
        response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT, verify=False)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        raise ValueError(f"No se pudo acceder a la URL: {e}") from e

    raw_bytes = response.content
    soup = BeautifulSoup(raw_bytes, "lxml")

    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else url

    content_text = trafilatura.extract(
        raw_bytes, url=url, include_comments=False, include_tables=True, favor_precision=True,
    ) or ""
    if not content_text.strip():
        body = soup.find("body")
        content_text = body.get_text(separator=" ", strip=True) if body else ""

    if not content_text.strip():
        raise ValueError("No se pudo extraer texto de la página")

    return {"title": title, "text": content_text}
