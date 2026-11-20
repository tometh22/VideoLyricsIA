"""One subtitle composition for the browser preview and the final video.

No image generation or ffmpeg work: return the actual ASS track, at the
output's logical resolution, to either native libass or its WASM build.
"""
import ass_render as ass


def compose_lyric_overlay(segments, *, width, height, text_scale, font_path,
                          case_fn, font_scale=1.0, lyric_transition="cut",
                          lyrics_animation="none", line_transition="none",
                          text_contrast="medium", lyric_color="", lyric_sung_color="",
                          look=None, look_opts=None, title_lines=None, beats=None, duration=None):
    title_lines = title_lines or []
    if look is not None:
        import lyric_looks
        import lyric_look_runtime
        return lyric_looks.build_look_ass(
            segments, look, width=width, height=height,
            duration=duration or max((float(s["end"]) for s in segments), default=1),
            case_fn=case_fn, font_scale=font_scale,
            primary_override=lyric_look_runtime.primary_override(look_opts, lyric_color),
            title_lines=title_lines, beats=beats or [],
        )
    family, bold = ass.font_family(font_path)
    factor = ass.font_size_factor(family)
    lines = ass.segments_to_lines(
        segments, text_scale=text_scale, font_scale=font_scale, font_factor=factor,
        lyric_transition=lyric_transition, animation=lyrics_animation,
        transition=line_transition, case_fn=case_fn,
    )
    stroke = {"subtle": 1.5, "medium": 2.5, "strong": 3.5}.get(text_contrast, 2.5)
    karaoke = lyrics_animation == "karaoke"
    return ass.build_ass(
        width=width, height=height, font_name=family, bold=bold,
        base_fontsize=ass.lyric_fontsize(40, text_scale, font_scale, factor),
        outline=max(1.0, stroke * text_scale), shadow=max(1, round(3 * text_scale)),
        lines=lines + title_lines,
        primary_color=(lyric_sung_color if karaoke else lyric_color) or "",
        secondary_color=(lyric_color if karaoke else "") or "",
    )
