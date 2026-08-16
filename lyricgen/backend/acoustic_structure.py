"""Text-independent vocal-event discovery for unsafe lyric windows.

The important contract in this module is ordering: waveform structure is
estimated before any ASR/Gemini text is inspected.  Content hypotheses may
later be mapped onto one of the acoustic N-best partitions, but they cannot
choose candidate boundaries or force a periodic/cardinality grid.

This is deliberately a bounded CPU implementation.  It operates on at most a
45 second window and degrades to a diagnostic decline instead of changing
lyrics when evidence is missing or ambiguous.
"""
from __future__ import annotations

import hashlib
import io
import itertools
import json
import logging
import math
import os
import re
import unicodedata
from typing import Iterable

import librosa
import numpy as np


logger = logging.getLogger("genly.acoustic_structure")
POLICY_VERSION = "acoustic-dp-v1"
SAMPLE_RATE = 16_000
HOP_LENGTH = 160                 # 10 ms boundary resolution
EMBEDDING_HOP = 320              # 20 ms feature resolution
MAX_WINDOW_SECONDS = 45.0
CONTEXT_SECONDS = 3.0
_EPS = 1e-8
_VOCALIZATIONS = {
    "ah", "aha", "eh", "hey", "oh", "ooh", "oooh", "uh", "uoh",
    "uoo", "uou", "woah", "wow", "yeah", "nooo", "noooo",
}


def _finite(value, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _normalize(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=np.float32)
    if not values.size:
        return values
    low, high = np.percentile(values, [5, 95])
    return np.clip((values - low) / (high - low + _EPS), 0.0, 1.0)


def _zscore_rows(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=np.float32)
    return (values - values.mean(axis=1, keepdims=True)) / (
        values.std(axis=1, keepdims=True) + 1e-6
    )


def _frame(seconds: float, *, hop: int = HOP_LENGTH) -> int:
    return max(0, int(round(seconds * SAMPLE_RATE / hop)))


def _tokenize(text: str) -> list[str]:
    text = unicodedata.normalize("NFD", str(text or "").casefold())
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return re.findall(r"[a-z0-9]+", text)


def content_class(text: str, kind: str = "") -> str:
    tokens = _tokenize(text)
    kind = str(kind or "").lower()
    if kind == "vocalization" or (tokens and all(
        token in _VOCALIZATIONS or re.fullmatch(r"[aeiou]+", token)
        for token in tokens
    )):
        return "NONLEXICAL"
    if len(tokens) == 1 and (
        len(tokens[0]) >= 5 and len(set(tokens[0][-4:])) <= 2
    ):
        return "SUSTAINED"
    return "LEXICAL" if tokens else "BLANK"


def _extract_view(path: str, start: float, end: float) -> dict | None:
    if not path or not os.path.exists(path) or end <= start:
        return None
    try:
        y, _ = librosa.load(
            path, sr=SAMPLE_RATE, mono=True, offset=max(0.0, start),
            duration=min(MAX_WINDOW_SECONDS, end - start),
        )
        if len(y) < SAMPLE_RATE // 2:
            return None
        mel = librosa.feature.melspectrogram(
            y=y, sr=SAMPLE_RATE, n_fft=1024, hop_length=EMBEDDING_HOP,
            n_mels=64, fmin=60, fmax=7600, power=2.0,
        )
        pcen = librosa.pcen(mel * (2 ** 31), sr=SAMPLE_RATE,
                           hop_length=EMBEDDING_HOP)
        log_mel = librosa.power_to_db(mel, ref=np.max)
        mfcc = librosa.feature.mfcc(S=log_mel, n_mfcc=20)
        delta = librosa.feature.delta(mfcc, width=9)
        delta2 = librosa.feature.delta(mfcc, width=9, order=2)
        chroma = librosa.feature.chroma_stft(
            y=y, sr=SAMPLE_RATE, n_fft=1024, hop_length=EMBEDDING_HOP,
        )
        rms = librosa.feature.rms(
            y=y, frame_length=1024, hop_length=HOP_LENGTH,
        )[0]
        onset = librosa.onset.onset_strength(
            y=y, sr=SAMPLE_RATE, hop_length=HOP_LENGTH,
        )
        flatness = librosa.feature.spectral_flatness(
            y=y, n_fft=1024, hop_length=HOP_LENGTH,
        )[0]
        harmonic, percussive = librosa.effects.hpss(y)
        harmonic_rms = librosa.feature.rms(
            y=harmonic, frame_length=1024, hop_length=HOP_LENGTH,
        )[0]
        percussive_rms = librosa.feature.rms(
            y=percussive, frame_length=1024, hop_length=HOP_LENGTH,
        )[0]
        harmonicity = harmonic_rms / (harmonic_rms + percussive_rms + _EPS)
        try:
            f0, voiced_flag, voiced_prob = librosa.pyin(
                y, fmin=65, fmax=1000, sr=SAMPLE_RATE,
                frame_length=2048, hop_length=HOP_LENGTH,
            )
            pitch = np.nan_to_num(np.log2(np.maximum(f0, 65) / 65), nan=0.0)
            voicing = np.where(voiced_flag, np.nan_to_num(voiced_prob), 0.0)
        except Exception:
            pitches, magnitudes = librosa.piptrack(
                y=y, sr=SAMPLE_RATE, n_fft=2048, hop_length=HOP_LENGTH,
                fmin=65, fmax=1000,
            )
            best = np.argmax(magnitudes, axis=0)
            raw_pitch = pitches[best, np.arange(pitches.shape[1])]
            voicing = magnitudes[best, np.arange(magnitudes.shape[1])]
            voicing = _normalize(voicing)
            pitch = np.where(
                voicing >= 0.08,
                np.log2(np.maximum(raw_pitch, 65) / 65), 0.0,
            )

        embed_n = min(mfcc.shape[1], delta.shape[1], delta2.shape[1],
                      chroma.shape[1], pcen.shape[1])
        embedding = np.vstack([
            _zscore_rows(mfcc[:, :embed_n]),
            .65 * _zscore_rows(delta[:, :embed_n]),
            .35 * _zscore_rows(delta2[:, :embed_n]),
            .35 * _zscore_rows(chroma[:, :embed_n]),
            .25 * _zscore_rows(pcen[:, :embed_n]),
        ]).astype(np.float32)
        boundary_n = min(len(rms), len(onset), len(flatness), len(harmonicity),
                         len(pitch), len(voicing))
        return {
            "embedding": embedding,
            "rms": _normalize(rms[:boundary_n]),
            "onset": _normalize(onset[:boundary_n]),
            "flatness": _normalize(flatness[:boundary_n]),
            "harmonicity": _normalize(harmonicity[:boundary_n]),
            "pitch": np.asarray(pitch[:boundary_n], dtype=np.float32),
            "voicing": _normalize(voicing[:boundary_n]),
        }
    except Exception as exc:
        logger.warning("[ACOUSTIC-STRUCTURE] feature extraction failed path=%s: %r",
                       path, exc)
        return None


def _cached_view(path: str, role: str, start: float, end: float, *,
                 cache=None, audio_hash: str = "", release: str = "") -> tuple[dict | None, bool]:
    """Load/store numerical front-end features with complete lineage."""
    address = None
    if cache is not None and audio_hash:
        try:
            from quality_cache import ArtifactKind, QualityCacheAddress
            address = QualityCacheAddress(
                artifact=ArtifactKind.FEATURES,
                audio_hash=audio_hash,
                model={"frontend": POLICY_VERSION, "role": role},
                config={
                    "window": [round(start, 3), round(end, 3)],
                    "sample_rate": SAMPLE_RATE, "boundary_hop": HOP_LENGTH,
                    "embedding_hop": EMBEDDING_HOP,
                },
                release=release or "unknown",
                lineage={
                    "view": role,
                    "source": "demucs_vocal_stem" if role == "stem" else "original_mix",
                    "separator_model": (
                        os.environ.get("REPLICATE_DEMUCS_MODEL")
                        or os.environ.get("DEMUCS_MODEL") or "unknown"
                    ) if role == "stem" else None,
                    "separator_version": os.environ.get(
                        "DEMUCS_MODEL_VERSION", "unknown"
                    ) if role == "stem" else None,
                    "separator_checksum": os.environ.get(
                        "DEMUCS_MODEL_CHECKSUM", "unknown"
                    ) if role == "stem" else None,
                },
            )
            payload = cache.get_bytes(address)
            if payload:
                with np.load(io.BytesIO(payload), allow_pickle=False) as stored:
                    return ({key: stored[key] for key in stored.files}, True)
        except Exception as exc:
            logger.warning("[ACOUSTIC-STRUCTURE] feature cache read declined: %r", exc)
    value = _extract_view(path, start, end)
    if value is not None and address is not None:
        try:
            buffer = io.BytesIO()
            np.savez_compressed(buffer, **value)
            cache.put_bytes(
                address, buffer.getvalue(),
                content_type="application/x-npz",
            )
        except Exception as exc:
            logger.warning("[ACOUSTIC-STRUCTURE] feature cache write declined: %r", exc)
    return value, False


def _boundary_candidates(stem: dict, mix: dict, start: float) -> list[dict]:
    """Return independently-scored start/end candidates; no text involved."""
    n = min(len(stem["rms"]), len(mix["rms"]), len(stem["onset"]),
            len(mix["onset"]))
    srms, mrms = stem["rms"][:n], mix["rms"][:n]
    son, mon = stem["onset"][:n], mix["onset"][:n]
    voice = np.clip(.55 * srms + .25 * stem["voicing"][:n]
                    + .20 * stem["harmonicity"][:n], 0, 1)
    rise = np.maximum(0.0, np.diff(voice, prepend=voice[0]))
    fall = np.maximum(0.0, -np.diff(voice, append=voice[-1]))
    pitch_delta = _normalize(np.abs(np.diff(stem["pitch"][:n], prepend=0.0)))
    spectral_delta = _normalize(np.abs(np.diff(stem["flatness"][:n], prepend=0.0)))
    novelty = np.clip(.35 * son + .20 * mon + .20 * pitch_delta
                      + .15 * spectral_delta + .10 * rise, 0, 1)

    raw_indices = set(np.flatnonzero(
        ((voice >= .24) & (np.roll(voice, 1) < .18))
        | ((voice < .18) & (np.roll(voice, 1) >= .24))
    ).tolist())
    for series in (novelty, son, fall):
        peaks = librosa.util.peak_pick(
            np.asarray(series), pre_max=3, post_max=3,
            pre_avg=10, post_avg=10, delta=.07, wait=6, sparse=True,
        )
        raw_indices.update(int(index) for index in peaks)

    candidates = []
    radius = _frame(.12)
    for index in sorted(i for i in raw_indices if 1 <= i < n - 1):
        lo, hi = max(0, index - radius), min(n, index + radius + 1)
        local_onset = max(float(np.max(son[lo:hi])), float(np.max(mon[lo:hi])))
        start_probability = np.clip(
            .40 * local_onset + .30 * float(rise[index])
            + .20 * float(novelty[index]) + .10 * float(mrms[index]), 0, 1,
        )
        end_probability = np.clip(
            .45 * float(fall[index]) + .30 * (1.0 - float(voice[index]))
            + .15 * float(novelty[index]) + .10 * (1.0 - float(mrms[index])), 0, 1,
        )
        if max(start_probability, end_probability) < .22:
            continue
        candidates.append({
            "time": round(start + index * HOP_LENGTH / SAMPLE_RATE, 3),
            "start_probability": round(float(start_probability), 4),
            "end_probability": round(float(end_probability), 4),
            "views": ["vocal_stem", "original_mix"],
            "scales": ["onset", "energy", "pitch", "timbre"],
        })

    # Non-maximum suppression is deliberately type-specific: a strong end and
    # a strong start at the same attack are causally different candidates.
    kept: list[dict] = []
    for kind in ("start_probability", "end_probability"):
        ranked = sorted(candidates, key=lambda item: item[kind], reverse=True)
        selected = []
        for item in ranked:
            if all(abs(item["time"] - old["time"]) >= .06 for old in selected):
                selected.append(item)
        for item in selected:
            existing = next((old for old in kept if old["time"] == item["time"]), None)
            if existing:
                existing[kind] = max(existing[kind], item[kind])
            else:
                kept.append(dict(item))
    return sorted(kept, key=lambda item: item["time"])


def _primitive_regions(stem: dict, mix: dict, window_start: float,
                       boundaries: list[dict] | None = None) -> list[tuple[float, float]]:
    """Fine vocal regions from hysteretic activity; gaps remain explicit."""
    n = min(
        len(stem["rms"]), len(stem["voicing"]), len(stem["harmonicity"]),
        len(stem["flatness"]), len(mix["rms"]), len(mix["voicing"]),
        len(mix["harmonicity"]), len(mix["flatness"]),
    )
    # Energy by itself is not vocal evidence: drums and bleed in an imperfect
    # Demucs stem otherwise become false lyric events. Gate each view by
    # voicing/harmonicity, while keeping a smaller mix contribution so crowd
    # calls removed by source separation remain visible.
    stem_likelihood = np.clip(
        .45 * stem["voicing"][:n] + .35 * stem["harmonicity"][:n]
        + .20 * (1.0 - stem["flatness"][:n]), 0, 1,
    )
    mix_likelihood = np.clip(
        .45 * mix["voicing"][:n] + .35 * mix["harmonicity"][:n]
        + .20 * (1.0 - mix["flatness"][:n]), 0, 1,
    )
    stem_activity = stem["rms"][:n] * (.05 + .95 * stem_likelihood)
    mix_activity = mix["rms"][:n] * (.04 + .96 * mix_likelihood)
    activity = np.clip(.78 * stem_activity + .22 * mix_activity, 0, 1)
    active = False
    begin = 0
    regions = []
    hang = _frame(.10)
    quiet_run = 0
    for index, value in enumerate(activity):
        if not active and value >= .14:
            active, begin, quiet_run = True, max(0, index - 2), 0
        elif active:
            quiet_run = quiet_run + 1 if value < .075 else 0
            if quiet_run >= hang:
                finish = index - quiet_run + 2
                a = window_start + begin * HOP_LENGTH / SAMPLE_RATE
                b = window_start + finish * HOP_LENGTH / SAMPLE_RATE
                if b - a >= .18:
                    regions.append((a, b))
                active, quiet_run = False, 0
    if active:
        a = window_start + begin * HOP_LENGTH / SAMPLE_RATE
        b = window_start + n * HOP_LENGTH / SAMPLE_RATE
        if b - a >= .18:
            regions.append((a, b))

    # Merge only micro-gaps.  Larger gaps are evidence for the partition DP,
    # not something a VAD post-process is allowed to erase.
    merged: list[tuple[float, float]] = []
    for a, b in regions:
        if merged and a - merged[-1][1] <= .16:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))

    # A sustained active region may still contain several phrases with no
    # silence between them. Split only on a strong, jointly start/end-like
    # acoustic reset; syllable onsets stay below this conservative threshold.
    split_regions: list[tuple[float, float]] = []
    for a, b in merged:
        cuts = sorted(
            _finite(item.get("time")) for item in (boundaries or [])
            if b - a >= 2.0
            and a + .45 <= _finite(item.get("time")) <= b - .45
            and _finite(item.get("start_probability")) >= .60
            and _finite(item.get("end_probability")) >= .25
        )
        selected: list[float] = []
        for cut in cuts:
            if not selected or cut - selected[-1] >= .45:
                selected.append(cut)
        cursor = a
        for cut in selected:
            split_regions.append((cursor, cut))
            cursor = cut
        split_regions.append((cursor, b))
    return split_regions


def _event_embedding(view: dict, start: float, end: float,
                     window_start: float) -> np.ndarray:
    left = _frame(start - window_start, hop=EMBEDDING_HOP)
    right = max(left + 1, _frame(end - window_start, hop=EMBEDDING_HOP))
    return view["embedding"][:, left:right]


def _dtw(left: np.ndarray, right: np.ndarray) -> float:
    if min(left.shape[1], right.shape[1]) < 4:
        return 1.5
    try:
        cost, _ = librosa.sequence.dtw(
            X=left, Y=right, metric="cosine", global_constraints=True,
            band_rad=.25,
        )
        return float(cost[-1, -1] / max(left.shape[1], right.shape[1]))
    except Exception:
        return 1.5


def _classify_event(start: float, end: float, stem: dict, mix: dict,
                    window_start: float) -> tuple[dict, int]:
    left = _frame(start - window_start)
    right = max(left + 1, _frame(end - window_start))
    duration = end - start
    onset = stem["onset"][left:right]
    rms = stem["rms"][left:right]
    voicing = stem["voicing"][left:right]
    mix_voicing = mix["voicing"][left:right]
    nuclei = int(len(librosa.util.peak_pick(
        np.asarray(onset), pre_max=2, post_max=2, pre_avg=5, post_avg=5,
        delta=.10, wait=5, sparse=True,
    ))) if len(onset) >= 8 else int(duration >= .35)
    mean_voicing = float(np.mean(voicing)) if len(voicing) else 0.0
    stem_energy = float(np.mean(rms)) if len(rms) else 0.0
    mean_mix_voicing = float(np.mean(mix_voicing)) if len(mix_voicing) else 0.0
    short = np.clip((.70 - duration) / .55, 0, 1)
    sustained = np.clip((duration - 1.20) / 3.0, 0, 1) * (1.0 - min(1, nuclei / 5))
    # Features are independently normalized per view, so subtracting their
    # RMS values is not a meaningful crowd score. Relative voicing is the
    # conservative cue: extra pitched voice in the mix can indicate chorus or
    # audience, but remains correlated evidence rather than a second witness.
    crowd = np.clip((mean_mix_voicing - mean_voicing + .10) / .50, 0, 1)
    lexical = np.clip(.25 + .16 * nuclei + .28 * mean_voicing, 0, 1)
    nonlexical = np.clip(.20 + .35 * short + .35 * sustained, 0, 1)
    raw = {
        "silence": max(0.0, .35 - max(stem_energy, mean_voicing)),
        "short_vocalization": float(short * nonlexical),
        "sustained_vocalization": float(sustained),
        "lexical_phrase": float(lexical),
        "crowd_or_overlap": float(crowd),
    }
    total = sum(raw.values()) + _EPS
    return ({key: round(value / total, 4) for key, value in raw.items()}, nuclei)


def _partition_candidates(primitives: list[tuple[float, float]], stem: dict,
                          mix: dict, window_start: float,
                          *, n_best: int = 32) -> list[dict]:
    if not primitives:
        return []
    # Beam-search a semi-Markov lattice.  An edge may consume 1-3 adjacent
    # primitive regions; its cost is acoustic continuity, never text count.
    beams: dict[int, list[tuple[float, list[tuple[int, int]]]]] = {0: [(0.0, [])]}
    for position in range(len(primitives)):
        for score, edges in beams.get(position, []):
            for width in range(1, min(6, len(primitives) - position) + 1):
                last = position + width - 1
                start, end = primitives[position][0], primitives[last][1]
                if end - start > 10.0:
                    continue
                gaps = [primitives[i + 1][0] - primitives[i][1]
                        for i in range(position, last)]
                if gaps and max(gaps) > 1.15:
                    continue
                merge_cost = 0.0
                for gap in gaps:
                    # Merging across a real pause is possible but expensive;
                    # this is what preserves irregular short calls such as
                    # ``no`` instead of swallowing them into a periodic motif.
                    merge_cost += .18 + min(1.4, gap * 1.45)
                duration_cost = .10 * max(0.0, .18 - (end - start))
                edge_score = score + merge_cost + duration_cost
                beams.setdefault(last + 1, []).append(
                    (edge_score, [*edges, (position, last + 1)]),
                )
        if position + 1 in beams:
            beams[position + 1] = sorted(beams[position + 1], key=lambda x: x[0])[:40]

    all_partitions = []
    seen = set()
    for score, edges in sorted(
        beams.get(len(primitives), []), key=lambda x: x[0]
    ):
        key = tuple(edges)
        if key in seen:
            continue
        seen.add(key)
        events = []
        for event_index, (left, right) in enumerate(edges):
            start, end = primitives[left][0], primitives[right - 1][1]
            posterior, nuclei = _classify_event(
                start, end, stem, mix, window_start,
            )
            confidence = 1.0 - min(1.0, max(.0, score) / max(1, len(edges)))
            events.append({
                "id": f"ae{event_index}", "start": round(start, 3),
                "end": round(end, 3), "type_posterior": posterior,
                "syllabic_nuclei": nuclei,
                "confidence": round(float(confidence), 4),
                "confidence_kind": "heuristic_partition_score",
                "primitive_ids": list(range(left, right)),
            })
        all_partitions.append({
            "rank": 0, "score": round(float(score), 5),
            "event_count": len(events), "events": events,
        })
    # Preserve structural alternatives, not just sub-frame variants of the
    # same cardinality.  Otherwise the cheapest all-split partition plus seven
    # nearly identical one-merge paths can crowd every two-merge hypothesis
    # out of N-best before content mapping gets a chance to compare them.
    selected = []
    counts = sorted({part["event_count"] for part in all_partitions}, reverse=True)
    grouped = {
        count: [part for part in all_partitions if part["event_count"] == count]
        for count in counts
    }
    # First preserve several topologically distinct paths per cardinality;
    # then fill by global score.  A single cheapest partition per count is not
    # enough when a context phrase and a refrain differ only in which adjacent
    # vocal regions should merge.
    for alternative in range(3):
        for count in counts:
            if alternative < len(grouped[count]):
                selected.append(grouped[count][alternative])
            if len(selected) >= n_best:
                break
        if len(selected) >= n_best:
            break
    for part in all_partitions:
        if part not in selected:
            selected.append(part)
        if len(selected) >= n_best:
            break
    partitions = sorted(selected[:n_best], key=lambda item: item["score"])
    for rank, partition in enumerate(partitions, 1):
        partition["rank"] = rank
    return partitions


def _refine_partition_starts(partitions: list[dict], boundaries: list[dict]) -> None:
    """Snap coarse VAD starts to local acoustic attacks, still without text."""
    for partition in partitions:
        for event in partition.get("events") or []:
            coarse = _finite(event.get("start"))
            end = _finite(event.get("end"))
            candidates = [
                item for item in boundaries
                if coarse - .10 <= _finite(item.get("time")) <= coarse + .80
                and _finite(item.get("time")) <= end - .12
                and _finite(item.get("start_probability")) >= .30
            ]
            if not candidates:
                continue
            selected = max(
                candidates,
                key=lambda item: (
                    _finite(item.get("start_probability")),
                    -abs(_finite(item.get("time")) - coarse),
                ),
            )
            refined = _finite(selected.get("time"), coarse)
            if refined > coarse + .02:
                event["coarse_start"] = event["start"]
                event["start"] = round(refined, 3)
                event["start_boundary_probability"] = selected.get(
                    "start_probability"
                )


def _motif_groups(events: list[dict], stem: dict, mix: dict,
                  window_start: float) -> list[dict]:
    links = []
    for left, right in itertools.combinations(range(len(events)), 2):
        a, b = events[left], events[right]
        duration_ratio = min(a["end"] - a["start"], b["end"] - b["start"]) / (
            max(a["end"] - a["start"], b["end"] - b["start"]) + _EPS
        )
        if duration_ratio < .55:
            continue
        stem_dtw = _dtw(
            _event_embedding(stem, a["start"], a["end"], window_start),
            _event_embedding(stem, b["start"], b["end"], window_start),
        )
        mix_dtw = _dtw(
            _event_embedding(mix, a["start"], a["end"], window_start),
            _event_embedding(mix, b["start"], b["end"], window_start),
        )
        recurrence = math.exp(-(0.68 * stem_dtw + .32 * mix_dtw))
        if recurrence >= .62:
            links.append((left, right, recurrence, stem_dtw, mix_dtw))
    groups: list[set[int]] = []
    for left, right, *_ in links:
        touched = [group for group in groups if left in group or right in group]
        if not touched:
            groups.append({left, right})
        else:
            merged = {left, right}
            for group in touched:
                merged |= group
                groups.remove(group)
            groups.append(merged)
    output = []
    for index, group in enumerate(groups):
        group_links = [link for link in links if link[0] in group and link[1] in group]
        output.append({
            "id": f"mg{index}",
            "event_ids": [events[i]["id"] for i in sorted(group)],
            "recurrence": round(float(np.mean([x[2] for x in group_links])), 4),
            "stem_dtw": round(float(np.mean([x[3] for x in group_links])), 4),
            "mix_dtw": round(float(np.mean([x[4] for x in group_links])), 4),
            "temporal_variation": round(float(np.std([
                events[i]["end"] - events[i]["start"] for i in group
            ])), 4),
            "hierarchy": "event",
        })
    return output


def _self_similarity_summary(stem: dict, mix: dict) -> dict:
    """Construct multiscale acoustic self-similarity without lyric text.

    Full matrices are intentionally not serialized into Job JSON. Their
    digest, dimensions, recurrence density and strongest non-trivial lags are
    enough for audit/repro; cached feature arrays can reconstruct them exactly.
    """
    n = min(stem["embedding"].shape[1], mix["embedding"].shape[1])
    if n < 8:
        return {"available": False, "reason": "insufficient_frames"}
    features = np.vstack([
        stem["embedding"][:, :n], .55 * mix["embedding"][:, :n],
    ]).astype(np.float32)
    scales = {
        "phonetic": 60,
        "syllabic": 180,
        "event": 600,
        "phrase": 1800,
    }
    output = {}
    for name, scale_ms in scales.items():
        frames = max(1, int(round(scale_ms / 20.0)))
        bins = n // frames
        if bins < 3:
            continue
        pooled = features[:, :bins * frames].reshape(
            features.shape[0], bins, frames,
        ).mean(axis=2)
        norms = np.linalg.norm(pooled, axis=0, keepdims=True) + _EPS
        normalized = pooled / norms
        matrix = np.clip(normalized.T @ normalized, -1.0, 1.0)
        affinity = ((matrix + 1.0) / 2.0).astype(np.float32)
        exclusion = max(1, int(round(300 / scale_ms)))
        mask = np.ones_like(affinity, dtype=bool)
        for offset in range(-exclusion, exclusion + 1):
            diagonal = np.arange(max(0, -offset), min(bins, bins - offset))
            mask[diagonal, diagonal + offset] = False
        values = affinity[mask]
        lag_scores = []
        for lag in range(exclusion + 1, bins):
            diagonal = np.diag(affinity, k=lag)
            if diagonal.size:
                lag_scores.append((float(np.mean(diagonal)), lag))
        strongest = sorted(lag_scores, reverse=True)[:5]
        output[name] = {
            "bin_ms": scale_ms,
            "shape": [bins, bins],
            "matrix_sha256": hashlib.sha256(
                np.round(affinity, 4).tobytes()
            ).hexdigest(),
            "recurrence_density": round(
                float(np.mean(values >= .82)) if values.size else 0.0, 5,
            ),
            "strongest_lags": [
                {"lag_s": round(lag * scale_ms / 1000.0, 3),
                 "affinity": round(score, 4)}
                for score, lag in strongest
            ],
        }
    return {"available": bool(output), "scales": output}


def analyze_window(stem_path: str, mix_path: str, *, window_start: float,
                   window_end: float, n_best: int = 32, cache=None,
                   audio_hash: str = "", stem_hash: str = "",
                   mix_hash: str = "", release: str = "") -> dict:
    """Build acoustic structure without observing lyric text/cardinality."""
    start = max(0.0, _finite(window_start))
    end = _finite(window_end)
    duration = end - start
    base = {
        "policy_version": POLICY_VERSION, "accepted": False,
        "reason": "invalid_window", "window": [round(start, 3), round(end, 3)],
        "boundaries": [], "best_partition": None, "n_best": [],
        "motif_groups": [], "cardinality_posterior": {},
    }
    if duration <= 0 or duration > MAX_WINDOW_SECONDS:
        return base
    stem, stem_cache_hit = _cached_view(
        stem_path, "stem", start, end, cache=cache,
        audio_hash=stem_hash or audio_hash, release=release,
    )
    mix, mix_cache_hit = _cached_view(
        mix_path, "mix", start, end, cache=cache,
        audio_hash=mix_hash or audio_hash, release=release,
    )
    if not stem or not mix:
        base["reason"] = "feature_extraction_failed"
        return base
    boundaries = None
    boundary_address = None
    if cache is not None and audio_hash:
        try:
            from quality_cache import ArtifactKind, QualityCacheAddress
            boundary_address = QualityCacheAddress(
                artifact=ArtifactKind.BOUNDARIES,
                audio_hash=audio_hash,
                model={"boundary_detector": POLICY_VERSION},
                config={"window": [round(start, 3), round(end, 3)]},
                release=release or "unknown",
                lineage={
                    "views": ["demucs_vocal_stem", "original_mix_correlated"],
                    "separator_model": (
                        os.environ.get("REPLICATE_DEMUCS_MODEL")
                        or os.environ.get("DEMUCS_MODEL") or "unknown"
                    ),
                    "separator_version": os.environ.get(
                        "DEMUCS_MODEL_VERSION", "unknown"
                    ),
                    "separator_checksum": os.environ.get(
                        "DEMUCS_MODEL_CHECKSUM", "unknown"
                    ),
                    "separator_variant": os.environ.get(
                        "DEMUCS_VARIANT", "unknown"
                    ),
                    "stem_sha256": stem_hash or "unknown",
                    "mix_sha256": mix_hash or audio_hash or "unknown",
                },
            )
            cached_boundaries = cache.get_json(boundary_address)
            if isinstance(cached_boundaries, list):
                boundaries = cached_boundaries
        except Exception as exc:
            logger.warning("[ACOUSTIC-STRUCTURE] boundary cache read declined: %r", exc)
    if boundaries is None:
        boundaries = _boundary_candidates(stem, mix, start)
        if boundary_address is not None:
            try:
                cache.put_json(boundary_address, boundaries)
            except Exception as exc:
                logger.warning("[ACOUSTIC-STRUCTURE] boundary cache write declined: %r", exc)
    primitives = _primitive_regions(stem, mix, start, boundaries)
    if len(primitives) > 32:
        base.update({
            "reason": "primitive_limit_exceeded",
            "boundaries": boundaries,
            "primitive_regions": [
                [round(a, 3), round(b, 3)] for a, b in primitives
            ],
            "diagnostics": {
                "text_independent": True,
                "primitive_count": len(primitives),
                "primitive_limit": 32,
                "fail_closed": True,
            },
        })
        return base
    partitions = _partition_candidates(primitives, stem, mix, start, n_best=n_best)
    if not partitions:
        base.update({"reason": "no_vocal_events", "boundaries": boundaries})
        return base
    _refine_partition_starts(partitions, boundaries)
    window_id = hashlib.sha256(
        f"{start:.3f}:{end:.3f}:{POLICY_VERSION}".encode("utf-8")
    ).hexdigest()[:10]
    for partition in partitions:
        for event_index, event in enumerate(partition.get("events") or []):
            event["id"] = (
                f"ae_{window_id}_{int(partition.get('rank') or 0)}_"
                f"{event_index}"
            )
    weights = np.exp(-np.asarray([part["score"] for part in partitions], dtype=float))
    weights /= float(np.sum(weights) + _EPS)
    cardinality: dict[int, float] = {}
    for weight, partition in zip(weights, partitions):
        count = int(partition["event_count"])
        cardinality[count] = cardinality.get(count, 0.0) + float(weight)
    best = partitions[0]
    motifs = _motif_groups(best["events"], stem, mix, start)
    self_similarity = _self_similarity_summary(stem, mix)
    base.update({
        "accepted": True, "reason": "analyzed",
        "boundaries": boundaries,
        "primitive_regions": [[round(a, 3), round(b, 3)] for a, b in primitives],
        "best_partition": best, "n_best": partitions,
        "motif_groups": motifs,
        "self_similarity": self_similarity,
        "cardinality_posterior": {
            str(key): round(value, 5) for key, value in sorted(cardinality.items())
        },
        "diagnostics": {
            "text_independent": True,
            "views_are_correlated": True,
            "cardinality_calibrated": False,
            "confidence_calibrated": False,
            "boundary_resolution_ms": 10,
            "embedding_resolution_ms": 20,
            "feature_cache_hits": {
                "stem": stem_cache_hit, "mix": mix_cache_hit,
            },
        },
    })
    return base


def _mapping_cost(event: dict, content: dict) -> float:
    event_duration = max(.01, _finite(event.get("end")) - _finite(event.get("start")))
    content_start, content_end = _finite(content.get("start")), _finite(content.get("end"))
    overlap = max(0.0, min(event["end"], content_end) - max(event["start"], content_start))
    union = max(event["end"], content_end) - min(event["start"], content_start)
    timing = 1.0 - overlap / max(.01, union)
    klass = content_class(content.get("text", ""), content.get("kind", ""))
    posterior = event.get("type_posterior") or {}
    if klass == "NONLEXICAL":
        class_support = max(_finite(posterior.get("short_vocalization")),
                            _finite(posterior.get("sustained_vocalization")))
    elif klass == "SUSTAINED":
        class_support = _finite(posterior.get("sustained_vocalization"))
    elif klass == "BLANK":
        class_support = _finite(posterior.get("silence"))
    else:
        class_support = _finite(posterior.get("lexical_phrase"))
    duration_penalty = max(0.0, .10 - event_duration)
    return .62 * timing + .33 * (1.0 - class_support) + .05 * duration_penalty


def _align_partition(events: list[dict], contents: list[dict]) -> dict:
    """Monotonic DP with explicit UNKNOWN/BLANK operations."""
    n, m = len(events), len(contents)
    dp = np.full((n + 1, m + 1), np.inf, dtype=float)
    back: dict[tuple[int, int], tuple[int, int, str]] = {}
    dp[0, 0] = 0.0
    for i in range(n + 1):
        for j in range(m + 1):
            current = dp[i, j]
            if not math.isfinite(float(current)):
                continue
            if i < n and current + .55 < dp[i + 1, j]:
                dp[i + 1, j] = current + .55
                back[i + 1, j] = (i, j, "UNKNOWN")
            if j < m and current + 1.0 < dp[i, j + 1]:
                dp[i, j + 1] = current + 1.0
                back[i, j + 1] = (i, j, "BLANK")
            if i < n and j < m:
                cost = current + _mapping_cost(events[i], contents[j])
                if cost < dp[i + 1, j + 1]:
                    dp[i + 1, j + 1] = cost
                    back[i + 1, j + 1] = (i, j, "MATCH")
    i, j = n, m
    assignments = []
    while i or j:
        previous = back.get((i, j))
        if previous is None:
            break
        pi, pj, operation = previous
        assignments.append({
            "operation": operation,
            "event_index": pi if i > pi else None,
            "content_index": pj if j > pj else None,
        })
        i, j = pi, pj
    assignments.reverse()
    return {"cost": round(float(dp[n, m]), 6), "assignments": assignments}


def map_content(structure: dict, hypotheses: Iterable[dict | list[dict]]) -> dict:
    """Map N-best content onto N-best acoustic partitions, contrastively."""
    candidates = []
    normalized_hypotheses = []
    for index, hypothesis in enumerate(hypotheses or []):
        if isinstance(hypothesis, dict):
            events = hypothesis.get("events") or []
            source = str(hypothesis.get("source") or f"source_{index}")
            family = str(hypothesis.get("family") or source)
        else:
            events, source, family = hypothesis, f"source_{index}", f"source_{index}"
        clean = [dict(event) for event in events if isinstance(event, dict)
                 and str(event.get("text") or "").strip()]
        if clean:
            normalized_hypotheses.append((source, family, clean))
    for partition in structure.get("n_best") or []:
        events = partition.get("events") or []
        for source, family, contents in normalized_hypotheses:
            aligned = _align_partition(events, contents)
            candidates.append({
                **aligned, "partition_rank": partition.get("rank"),
                "partition_score": partition.get("score"),
                "source": source, "family": family,
                "events": events, "content": contents,
                "total_cost": round(
                    aligned["cost"] + .10 * _finite(partition.get("score")), 6,
                ),
            })
    candidates.sort(key=lambda item: item["total_cost"])
    if not candidates:
        return {"accepted": False, "reason": "content_unavailable", "n_best": []}
    best = candidates[0]

    # Build complete, auditable sequence alternatives for the trusted CTC
    # layer. Only one-to-one candidates can be phonemically compared; UNKNOWN
    # and BLANK paths remain diagnostics but can never certify text.
    phonetic_candidates = []
    seen_phonetic = set()
    selected_candidate_id = ""
    for candidate in candidates:
        texts = [None] * len(candidate["events"])
        complete = True
        for assignment in candidate["assignments"]:
            event_index = assignment.get("event_index")
            content_index = assignment.get("content_index")
            if (
                assignment.get("operation") != "MATCH"
                or event_index is None or content_index is None
            ):
                complete = False
                break
            texts[event_index] = str(
                candidate["content"][content_index].get("text") or ""
            ).strip()
        if not complete or not texts or any(not text for text in texts):
            continue
        anchors = [round(_finite(event.get("start")), 3)
                   for event in candidate["events"]]
        semantic_key = (
            tuple(" ".join(text.lower().split()) for text in texts),
            tuple(anchors),
        )
        if semantic_key in seen_phonetic:
            continue
        seen_phonetic.add(semantic_key)
        encoded = json.dumps(
            {"texts": texts, "anchors": anchors}, ensure_ascii=False,
            sort_keys=True, separators=(",", ":"),
        )
        candidate_id = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]
        item = {
            "candidate_id": candidate_id,
            "texts": texts, "anchors": anchors,
            "source": candidate["source"], "family": candidate["family"],
            "partition_rank": candidate["partition_rank"],
            "mapping_cost": candidate["total_cost"],
        }
        phonetic_candidates.append(item)
        if candidate is best:
            selected_candidate_id = candidate_id
        if len(phonetic_candidates) >= 8:
            break
    # Alternatives from the same model family are correlated and cannot create
    # a false consensus margin.  Keep the closest genuinely distinct topology
    # or evidence family for uncertainty measurement.
    second = next((candidate for candidate in candidates[1:] if
                   candidate["partition_rank"] != best["partition_rank"] or
                   candidate["family"] != best["family"]), None)
    margin = (second["total_cost"] - best["total_cost"]) if second else 0.0
    mapped_events = []
    unassigned = []
    for assignment in best["assignments"]:
        event_index = assignment.get("event_index")
        content_index = assignment.get("content_index")
        if event_index is None:
            continue
        event = dict(best["events"][event_index])
        if assignment["operation"] == "MATCH" and content_index is not None:
            content = best["content"][content_index]
            tokens = _tokenize(content.get("text", ""))
            event.update({
                "text": str(content.get("text") or "").strip(),
                "content_class": content_class(
                    content.get("text", ""), content.get("kind", ""),
                ),
                "content_source": best["source"],
                "content_kind": str(content.get("kind") or ""),
                "content_start": round(_finite(content.get("start")), 4),
                "content_duration": round(max(
                    0.0, _finite(content.get("end")) - _finite(content.get("start")),
                ), 4),
                "has_vocalization_tail": bool(
                    len(tokens) >= 2
                    and any(token in _VOCALIZATIONS for token in tokens[1:])
                ),
            })
            mapped_events.append(event)
        else:
            unassigned.append(event)
    # The content DP may choose among already-discovered acoustic attacks; it
    # may not create or move a boundary outside that lattice.  This is where a
    # CTC/Gemini/ASR onset helps disambiguate lead-in breath vs lexical attack
    # without feeding text cardinality back into structure discovery.
    boundaries = list(structure.get("boundaries") or [])
    for event in mapped_events:
        content_start = _finite(event.get("content_start"), float("nan"))
        if not math.isfinite(content_start):
            continue
        acoustic_candidates = [
            boundary for boundary in boundaries
            if _finite(boundary.get("start_probability")) >= .30
            and abs(_finite(boundary.get("time")) - content_start) <= .75
            and _finite(event.get("coarse_start"), _finite(event.get("start"))) - .15
            <= _finite(boundary.get("time"))
            <= _finite(event.get("end")) - .10
        ]
        if acoustic_candidates:
            selected = min(
                acoustic_candidates,
                key=lambda boundary: (
                    abs(_finite(boundary.get("time")) - content_start)
                    + .08 * (1.0 - _finite(boundary.get("start_probability"))),
                ),
            )
            event["start"] = round(_finite(selected.get("time")), 3)
            event["content_guided_boundary_probability"] = selected.get(
                "start_probability"
            )
            event["start_source"] = "acoustic_candidate_content_dp"

    # Preserve active-voice boundaries and derive the editor display end only
    # from the next *acoustically discovered* onset for lexical+vocalization
    # motifs.  This matches karaoke hold semantics without feeding text count
    # back into boundary discovery.  Very short nonlexical calls retain their
    # content duration as a conservative display bound while the full active
    # region remains available in ``acoustic_end`` for review.
    for index, event in enumerate(mapped_events):
        event["acoustic_start"] = event["start"]
        event["acoustic_end"] = event["end"]
        if event.get("has_vocalization_tail") and index + 1 < len(mapped_events):
            next_start = _finite(mapped_events[index + 1].get("start"))
            gap = next_start - _finite(event.get("end"))
            # Live arrangements commonly leave a two-beat breath before the
            # next call. The next onset is still acoustic evidence; allowing
            # up to 2.5 s preserves the sung hold without inventing a boundary.
            if 0.0 <= gap <= 2.5:
                event["end"] = round(next_start, 3)
                event["display_end_source"] = "next_acoustic_onset"
        elif (
            event.get("content_class") == "NONLEXICAL"
            and 0.0 < _finite(event.get("content_duration")) <= .5
        ):
            event["end"] = round(min(
                _finite(event.get("acoustic_end")),
                _finite(event.get("start")) + max(
                    .12, _finite(event.get("content_duration")),
                ),
            ), 3)
            event["display_end_source"] = "short_content_class"

    strong_unassigned = [event for event in unassigned if
                         _finite(event.get("confidence")) >= .55]
    topology_mapping_supported = margin >= .12 and not strong_unassigned
    # Caller-supplied dictionaries are not a trusted acoustic witness.  A
    # future singing-specific CTC/PPG scorer must compute and sign this result
    # inside this module before text can be certified automatically.  Until
    # then the DP output is deliberately suggestion-only.
    phonetic_verified = False
    accepted = False
    return {
        "accepted": accepted,
        "reason": "mapped" if accepted else (
            "strong_unassigned_events" if strong_unassigned
            else "ambiguous_mapping" if not topology_mapping_supported
            else "phonetic_evidence_unavailable"
        ),
        "topology_mapping_supported": topology_mapping_supported,
        "phonetic_verified": phonetic_verified,
        "margin": round(float(margin), 6),
        "events": mapped_events,
        "unassigned_events": unassigned,
        "strong_unassigned_events": len(strong_unassigned),
        "selected_candidate_id": selected_candidate_id,
        "phonetic_candidates": phonetic_candidates,
        "n_best": [{key: value for key, value in candidate.items()
                    if key not in {"events", "content"}}
                   for candidate in candidates[:8]],
        "evidence_lineage": [{"source": source, "family": family}
                             for source, family, _ in normalized_hypotheses],
    }


def analysis_fingerprint(audio_hash: str, model_identity: str,
                         config: dict, release: str) -> str:
    payload = repr((audio_hash, model_identity, sorted(config.items()), release,
                    POLICY_VERSION)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
