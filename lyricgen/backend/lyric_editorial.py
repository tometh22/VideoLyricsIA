"""Approved editorial looks: pure, per-render ASS composition.

All coordinates live in a 720-pixel short-side design space. The adapter scales
geometry (never timing) to the master/short resolution. No shared event buffers,
filesystem writes, audio access or prototype dependencies.
"""
from __future__ import annotations

import math
import os
import re

import ass_render as A
import lyric_looks as L


def _scale_text(body: str, scale: float) -> str:
    if scale == 1:
        return body
    number = lambda value: A._fmt_num(float(value) * scale)
    def vector(match):
        return match[1] + ','.join(number(n) for n in match[2].split(',')) + ')'
    def tags(match):
        block = re.sub(r"(\\(?:pos|org|clip)\()(-?[\d.]+(?:,-?[\d.]+){1,3})\)", vector, match[0])
        return re.sub(r"(\\(?:fs|bord|shad|blur))([\d.]+)", lambda m: m[1] + number(m[2]), block)
    body = re.sub(r"\{[^}]*\}", tags, body)
    if r"\p1}" in body:
        tags, drawing = body.split(r"\p1}", 1)
        drawing = re.sub(r"-?\d+(?:\.\d+)?", lambda m: number(m[0]), drawing)
        body = tags + r"\p1}" + drawing
    return body


class EditorialRenderer:
    def __init__(self, look, width, height, duration, font_scale=1.0):
        self.look = look
        self.scale = min(width, height) / 720.0
        self.w, self.h = round(width / self.scale), round(height / self.scale)
        self.portrait = height > width
        self.duration = duration
        self.font_scale = max(.6, min(1.5, float(font_scale or 1))) * look.font_scale
        self.main = look.font_file
        self.key = look.key_font or self.main
        self.small = look.extra_fonts[0] if look.extra_fonts else self.main
        self.light = look.extra_fonts[1] if len(look.extra_fonts) > 1 else self.small
        self.measures = {name: L._Measure(os.path.join(L._FONTS_DIR, name))
                         for name in {self.main, self.key, self.small, self.light}}
        self.faces = {name: A.font_family(os.path.join(L._FONTS_DIR, name)) for name in self.measures}
        self.events = []
        self.art_transform = None
        self.palettes = {look.layout: (look.primary, look.accent or look.primary)}

    def event(self, layer, start, end, body):
        # Brief syllables still reach full opacity; fixed prototype fades could
        # overlap each other when a word's complete window was <100 ms.
        fade_limit = max(0, int((end-start)*1000)//3)
        def fades(block):
            return re.sub(r"\\fad\((\d+),(\d+)\)",
                          lambda m: f"\\fad({min(int(m[1]), fade_limit)},{min(int(m[2]), fade_limit)})", block[0])
        body = re.sub(r"\{[^}]*\}", fades, body)
        if self.art_transform:
            scale, dx, dy = self.art_transform
            # Decorations consist of drawing paths plus optional rotation origins.
            def origin(m):
                return f"\\org({A._fmt_num(float(m[1])*scale+dx)},{A._fmt_num(float(m[2])*scale+dy)})"
            body = re.sub(r"\\org\((-?[\d.]+),(-?[\d.]+)\)", origin, body)
            if r"\p1}" in body:
                tags, drawing = body.split(r"\p1}", 1)
                axis = iter([dx, dy] * (len(re.findall(r"-?\d+(?:\.\d+)?", drawing)) // 2 + 1))
                drawing = re.sub(r"-?\d+(?:\.\d+)?", lambda m: A._fmt_num(float(m[0])*scale+next(axis)), drawing)
                body = tags + r"\p1}" + drawing
        line = L._dialogue(layer, start, end, _scale_text(body, self.scale))
        if line:
            self.events.append(line)

    def timing(self, seg):
        display = seg['display']
        tokens = display.split()
        timings = A._word_timings(display, seg['start'], seg['end'], seg.get('words'))
        # Preserve alignment, including late/short words. Never jump a late word
        # back to the start of the phrase.
        starts = [max(seg['start'], min(seg['end'], float(t['start']) - .035)) for t in timings]
        for i in range(1, len(starts)):
            starts[i] = max(starts[i - 1], starts[i])
        return tokens, starts

    starts = timing

    def text(self, layer, start, end, body, x, y, fs, font, color, extra='', align=5):
        (face, bold) = self.faces[font]
        tags = f'\\an{align}\\pos({round(x)},{round(y)})\\fn{face}\\b{(-1 if bold else 0)}\\fs{round(fs)}\\1c{L._c(color)}\\bord0\\shad0{extra}'
        self.event(layer, start, end, '{' + tags + '}' + A._ass_escape(body))

    def rect(self, layer, start, end, x, y, w, h, color, extra=''):
        self.event(layer, start, end, f'{{\\an7\\pos(0,0)\\bord0\\shad0\\1c{L._c(color)}{extra}\\p1}}m {round(x)} {round(y)} l {round(x + w)} {round(y)} {round(x + w)} {round(y + h)} {round(x)} {round(y + h)}')

    def fit(self, body, font, maxw, maxh, limit=900):
        m = self.measures[font]
        (lo, hi) = (10, max(11, round(limit * max(1, self.font_scale))))
        while hi - lo > 1:
            fs = (lo + hi) // 2
            if m.width(body, fs) <= maxw and m.ink_height(body, fs) <= maxh:
                lo = fs
            else:
                hi = fs
        return max(10, round(lo * min(1, self.font_scale)))

    def tag_motion(self, start, end, fade=False):
        dur = max(20, round((end - start) * 1000))
        enter = min(90, max(10, dur // 3))
        leave = min(90, max(10, dur // 5))
        if fade:
            return f'\\fad({enter},{leave})'
        return f'\\fscx92\\fscy92\\t(0,{enter},\\fscx100\\fscy100)\\fad(0,{leave})'

    def golpe(self, seg, n):
        (tokens, word_starts) = self.timing(seg)
        key = L.pick_keyword(tokens)
        (ink, accent) = self.palettes['golpe']
        end = seg['end']
        for (i, word) in enumerate(tokens):
            s = word_starts[i]
            e = word_starts[i + 1] if i + 1 < len(word_starts) else end
            if e - s < 0.02:
                continue
            color = accent if i == key else ink
            font = self.key if i == key else self.main
            fs = self.fit(word, font, self.w * 0.87, self.h * 0.42, 680)
            self.text(2, s, e, word, self.w / 2, self.h * 0.6, fs, font, color, f'\\1a&HFF&\\3c{L._c(color)}\\3a&HA0&\\bord1.4\\clip(30,110,{self.w - 30},{self.h - 90})' + self.tag_motion(s, e, True))
            self.text(3, s, e, word, self.w / 2, self.h * 0.43, fs, font, color, self.tag_motion(s, e))
            spoken = ' '.join(tokens[:i + 1])
            small = self.fit(spoken, self.small, self.w * 0.84, 31, 48)
            self.text(3, s, e, spoken, self.w / 2, self.h - 62, small, self.small, ink, align=5)

    def torn(self, layer, start, end, cx, cy, w, h, color, angle, seed, shadow=False):
        x = cx - w / 2
        y = cy - h / 2
        step = 32
        pts = []
        for i in range(int(w // step) + 1):
            px = min(w, i * step)
            j = (i * 19 + seed * 7) % 11 - 5
            pts.append((x + px, y + j))
        pts.extend([(x + w, y + 2), (x + w + 4, y + h * 0.52), (x + w, y + h)])
        for i in range(int(w // step), -1, -1):
            px = min(w, i * step)
            j = (i * 13 + seed * 11) % 13 - 6
            pts.append((x + px, y + h + j))
        if shadow:
            pts = [(a + 7, b + 9) for (a, b) in pts]
        draw = 'm ' + ' l '.join((f'{round(a)} {round(b)}' for (a, b) in pts))
        alpha = '\\1a&H50&' if shadow else ''
        self.event(layer, start, end, f'{{\\an7\\pos(0,0)\\org({round(cx)},{round(cy)})\\frz{angle}\\bord0\\shad0\\1c{L._c(color)}{alpha}\\fad(35,60)\\p1}}' + draw)

    def recorte(self, seg, n):
        (tokens, word_starts) = self.timing(seg)
        key = L.pick_keyword(tokens)
        (paper, red) = self.palettes['recorte']
        end = seg['end']
        rows = []
        cur = []
        limit = max(9, min(17, math.ceil(sum((len(t) + 1 for (i, t) in enumerate(tokens) if i != key)) / 2)))
        for (i, t) in enumerate(tokens):
            if i == key:
                if cur:
                    rows.append(cur)
                    cur = []
                rows.append([i])
                continue
            if cur and sum((len(tokens[j]) + 1 for j in cur)) + len(t) > limit:
                rows.append(cur)
                cur = []
            cur.append(i)
        if cur:
            rows.append(cur)
        weights = [1.35 if ids == [key] else 1 for ids in rows]
        unit = min(152, self.h * 0.73 / sum(weights))
        cursor = (self.h - unit * sum(weights)) / 2
        for (r, ids) in enumerate(rows):
            rh = unit * weights[r]
            phrase = ' '.join((tokens[i] for i in ids))
            is_key = ids == [key]
            font = self.key if is_key else self.main
            fs = self.fit(phrase, font, self.w * (0.78 if is_key else 0.7), rh * 0.66, 230)
            m = self.measures[font]
            space = max(m.width(' ', fs), fs * 0.22)
            ww = [m.width(tokens[i], fs) for i in ids]
            tw = sum(ww) + space * (len(ids) - 1)
            pw = tw + 70
            ph = m.ink_height(phrase, fs) + 40
            cx = self.w / 2 + [-0.045, 0.035, -0.025, 0.025][r % 4] * self.w
            cy = cursor + rh / 2
            cursor += rh
            angle = [-3.5, 3.2, -2.6, 2.8][(r + n) % 4]
            s = word_starts[ids[0]]
            self.torn(1, s, end, cx, cy, pw, ph, '#000000', angle, r + n, True)
            self.torn(2, s, end, cx, cy, pw, ph, red if is_key else paper, angle, r + n)
            x = cx - tw / 2
            for (i, wword) in zip(ids, ww):
                self.text(3, word_starts[i], end, tokens[i], x + wword / 2, cy, fs, font, paper if is_key else '#191A18', f'\\org({round(cx)},{round(cy)})\\frz{angle}\\fad(30,65)')
                x += wword + space

    def diptico(self, seg, n):
        tokens, word_starts = self.timing(seg)
        key = L.pick_keyword(tokens)
        paper, blue = self.look.line_colors[1], self.look.accent
        end = seg['end']
        panel = self.h*.30 if self.portrait else self.w*.30
        self.rect(0, seg['start'], end, 0, 0, self.w if self.portrait else panel,
                  panel if self.portrait else self.h, paper)
        self.rect(1, seg['start'], end, 0 if self.portrait else panel,
                  panel if self.portrait else 0, self.w if self.portrait else 3,
                  3 if self.portrait else self.h, blue)
        col_w = (self.w-84)/2 if self.portrait else panel-84
        fs = min(66, self.fit(max(tokens, key=len), self.small, col_w, 55, 80))
        row_count = math.ceil(len(tokens)/2) if self.portrait else len(tokens)
        row_h = min(64, ((panel-80) if self.portrait else (self.h-180))/row_count)
        top = ((panel if self.portrait else self.h)-row_h*row_count)/2
        for i, word in enumerate(tokens):
            s = word_starts[i]
            e = word_starts[i+1] if i+1<len(tokens) else end
            x = 42 + ((i % 2)*col_w if self.portrait else 0)
            cy = top + ((i//2 if self.portrait else i)+.5)*row_h
            self.text(3, s, end, word, x, cy, fs,
                      self.small if i==key else self.light, '#172B39', self.tag_motion(s,end,True), align=4)
            if e-s < .02:
                continue
            hero = word.title()
            font = self.key if i==key else self.main
            cx = self.w/2 if self.portrait else panel+(self.w-panel)/2
            cy = panel+(self.h-panel)*.40 if self.portrait else self.h*.45
            maxw = self.w*.84 if self.portrait else (self.w-panel)*.84
            hfs = self.fit(hero, font, maxw, self.h*.51, 650)
            self.text(3,s,e,hero,cx,cy,hfs,font,blue if i==key else self.look.primary,
                      r'\bord1\3c&H00312314&'+self.tag_motion(s,e,True))
            x = 60 if self.portrait else panel+60
            rail = self.w-120 if self.portrait else self.w-panel-120
            self.rect(2,s,e,x,self.h*.77,rail*(i+1)/len(tokens),3,blue)

    def shape(self, layer, s, e, path, color, extra=''):
        self.event(layer, s, e, f'{{\\an7\\pos(0,0)\\bord0\\shad0\\1c{L._c(color)}{extra}\\p1}}' + path)

    def ellipse(self, layer, s, e, cx, cy, rx, ry, color, extra=''):
        k = 0.55228475
        p = f'm {cx - rx:.1f} {cy:.1f} b {cx - rx:.1f} {cy - k * ry:.1f} {cx - k * rx:.1f} {cy - ry:.1f} {cx:.1f} {cy - ry:.1f} {cx + k * rx:.1f} {cy - ry:.1f} {cx + rx:.1f} {cy - k * ry:.1f} {cx + rx:.1f} {cy:.1f} {cx + rx:.1f} {cy + k * ry:.1f} {cx + k * rx:.1f} {cy + ry:.1f} {cx:.1f} {cy + ry:.1f} {cx - k * rx:.1f} {cy + ry:.1f} {cx - rx:.1f} {cy + k * ry:.1f} {cx - rx:.1f} {cy:.1f}'
        self.shape(layer, s, e, p, color, extra)

    def heart(self, layer, s, e, x, y, size, color, extra=''):
        path = f'm {x} {y + size * 0.8} b {x - size * 1.2} {y} {x - size * 0.6} {y - size * 0.85} {x} {y - size * 0.2} b {x + size * 0.6} {y - size * 0.85} {x + size * 1.2} {y} {x} {y + size * 0.8}'
        self.shape(layer, s, e, path, color, extra)

    def star(self, layer, s, e, x, y, r, color, extra=''):
        pts = []
        for i in range(10):
            a = -math.pi / 2 + i * math.pi / 5
            rad = r if i % 2 == 0 else r * 0.46
            pts.append((x + rad * math.cos(a), y + rad * math.sin(a)))
        self.shape(layer, s, e, 'm ' + ' l '.join((f'{a:.1f} {b:.1f}' for (a, b) in pts)), color, extra)

    def flower(self, layer, s, e, x, y, r, petal, center, extra=''):
        for i in range(5):
            a = i * 2 * math.pi / 5 - math.pi / 2
            self.ellipse(layer, s, e, x + math.cos(a) * r * 0.65, y + math.sin(a) * r * 0.65, r * 0.52, r * 0.52, petal, extra)
        self.ellipse(layer + 1, s, e, x, y, r * 0.35, r * 0.35, center, extra)

    def bounce(self, s, e):
        dur = round((e - s) * 1000)
        a = max(8, min(130, dur // 3))
        b = max(a + 1, min(240, dur // 2))
        return f'\\fscx75\\fscy75\\t(0,{a},\\fscx106\\fscy106)\\t({a},{b},\\fscx100\\fscy100)\\fad(0,{min(70, max(10, dur // 5))})'

    def stack(self, tokens, font, maxw, maxh, base, keyscale):
        base *= self.font_scale
        key = L.pick_keyword(tokens)
        sizes = [base * (keyscale if i == key else 1) for i in range(len(tokens))]
        rows = []
        cur = []
        used = 0
        for (i, t) in enumerate(tokens):
            width = self.measures[self.key if i == key else font].width(t, int(sizes[i]))
            gap = base * 0.2
            if cur and (i == key or cur[-1] == key or used + gap + width > maxw):
                rows.append(cur)
                cur = []
                used = 0
            cur.append(i)
            used += (gap if len(cur) > 1 else 0) + width
        if cur:
            rows.append(cur)
        heights = [max((sizes[i] for i in row)) * 0.86 + 18 for row in rows]
        scale = min(1, maxh / sum(heights), min((maxw / max(self.measures[self.key if i == key else font].width(tokens[i], int(sizes[i])), 1) for i in range(len(tokens)))))
        sizes = [max(18, round(fs * scale)) for fs in sizes]
        heights = [h * scale for h in heights]
        return (rows, sizes, heights, key)

    def diary_bg(self):
        self.flower(2, 0, self.duration, self.w*.0883, self.h*.164, 42, '#F3B3C7', '#FFDB81', r'\bord6\3c&H00FFFFFF&')
        self.heart(2, 0, self.duration, self.w*.897, self.h*.817, 40, '#F3A6B9', r'\bord6\3c&H00FFFFFF&')
        self.star(2, 0, self.duration, self.w*.877, self.h*.160, 32, '#D1BFE9', r'\bord6\3c&H00FFFFFF&')
        self.star(2, 0, self.duration, self.w*.121, self.h*.837, 25, '#F4C567', r'\bord5\3c&H00FFFFFF&')

    def diario(self, seg, n):
        (tokens, ts) = self.starts(seg)
        end = seg['end']
        (rows, sizes, rh, key) = self.stack(tokens, self.main, self.w * .695, min(600, self.h * .597), 150, 1.65)
        y = (self.h - sum(rh)) / 2
        pal = [self.look.primary, self.look.line_colors[1], self.look.accent]
        for (row_idx, ids) in enumerate(rows):
            widths = [self.measures[self.key if i == key else self.main].width(tokens[i], sizes[i]) for i in ids]
            gap = 26
            tw = sum(widths) + gap * (len(ids) - 1)
            x = (self.w - tw) / 2 + [-16, 18, -6][row_idx % 3]
            for (j, i) in enumerate(ids):
                px = x + widths[j] / 2
                py = y + rh[row_idx] / 2
                angle = [-3, 2, -1, 3][(i + n) % 4]
                extra = f"\\bord7\\3c{L._c('#FFFFFF')}\\shad3\\4c{L._c('#48384C')}\\4a&H65&\\frz{angle}" + self.bounce(ts[i], end)
                self.text(4, ts[i], end, tokens[i], px, py, sizes[i], self.key if i == key else self.main, self.look.accent if i == key and self.key != self.main else pal[(i + n) % 3], extra)
                if i == key:
                    self.heart(3, ts[i], end, min(self.w-28, px + widths[j] / 2 + 45), py - 4, 17, '#E99BB8', '\\bord3\\3c&H00FFFFFF&\\fad(80,60)')
                x += widths[j] + gap
            y += rh[row_idx]

    def cloud_bg(self):
        for (x, y, r) in [(0, 720, 170), (170, 760, 185), (355, 780, 150), (1040, 780, 180), (1260, 720, 180)]:
            self.ellipse(1, 0, self.duration, x*self.w/1280, y*self.h/720, r,  r * 0.72, '#EEE4FA', '\\1a&H65&')
        for (x, y, r) in [(25, 25, 145), (195, -20, 105), (1160, -25, 190)]:
            self.ellipse(1, 0, self.duration, x*self.w/1280, y*self.h/720, r,  r * 0.7, '#F3DAE7', '\\1a&H65&')
        for (x, y, r) in [(112, 239, 22), (1150, 240, 27), (1038, 575, 16), (233, 552, 17)]:
            x, y = x*self.w/1280, y*self.h/720
            self.star(2, 0, self.duration, x, y, r, '#FFF1B5', f'\\org({x},{y})\\frz-12\\t(0,14000,\\frz12)')

    def nube(self, seg, n):
        (tokens, ts) = self.starts(seg)
        end = seg['end']
        key = L.pick_keyword(tokens)
        ends = ts[1:] + [end]
        for (i, word) in enumerate(tokens):
            (s, e) = (ts[i], ends[i])
            if e - s < 0.02:
                continue
            font = self.key if i == key else self.main
            fs = self.fit(word, font, self.w * .75, min(360, self.h * .389), 600)
            motion = self.bounce(s, e)
            color = self.look.accent if i == key else self.look.primary
            self.text(3, s, e, word, self.w / 2, self.h * 0.47, fs, font, color, f"\\bord7\\3c{L._c('#9975B6')}\\shad9\\4c{L._c('#B696D0')}\\4a&H00&" + motion)
            self.text(4, s, e, word, self.w / 2, self.h * 0.47, fs, font, '#FFFFFF', f'\\1a&HB8&\\clip({round(self.w*.109)},80,{round(self.w*.891)},{round(self.h*.431)})' + motion)
            phrase = ' '.join(tokens[:i + 1])
            small = self.fit(phrase, self.small, self.w * .758, 32, 55)
            self.text(4, s, e, phrase, self.w / 2, self.h * .7875, small, self.small, '#FFF3F9', f"\\bord2\\3c{L._c('#44354F')}" + self.tag_motion(s, e, True))
            if i == key:
                for (x, y, r) in [(210, 270, 22), (1065, 375, 21), (1000, 165, 16)]:
                    x, y = x*self.w/1280, y*self.h/720
                    self.star(2, s, e, x, y, r, '#FFF6CA', f'\\org({x},{y})\\frz-15\\t(0,250,\\frz15)\\fad(50,80)')

    def cloth_bg(self):
        for x in range(36, self.w - 36, 23):
            for y in [30, self.h - 30]:
                self.rect(2, 0, self.duration, x, y, 12, 2, '#C8DED0', '\\1a&H35&')
        for y in range(45, self.h - 45, 23):
            for x in [30, self.w - 30]:
                self.rect(2, 0, self.duration, x, y, 2, 12, '#C8DED0', '\\1a&H35&')
        self.art_transform = (.65, self.w*.75-1030*.65, self.h*.83-451*.65) if self.portrait else None
        for (x, y, dx) in [(1030, 290, 20), (1137, 385, -25), (935, 465, 15)]:
            self.shape(2, 0, self.duration, f'm {x} {y} b {x + dx} {y + 80} {x - dx} 590 1030 622 l 1035 622 b {x - dx + 4} 590 {x + dx + 4} {y + 80} {x + 4} {y}', '#ACD4BC', '\\bord1\\3c&H00465D43&')
            self.ellipse(2, 0, self.duration, x - 16, y + 100, 32, 12, '#ACD4BC', f'\\org({x - 16},{y + 100})\\frz-25')
            self.ellipse(2, 0, self.duration, x + 22, y + 154, 34, 13, '#ACD4BC', f'\\org({x + 22},{y + 154})\\frz28')
        self.flower(3, 0, self.duration, 1030, 274, 67, '#E6ABAA', '#F2DB98')
        self.flower(3, 0, self.duration, 1137, 370, 47, '#E8D9B4', '#B8958B')
        self.flower(3, 0, self.duration, 935, 451, 42, '#B9B5D5', '#EEDBA3')
        self.art_transform = None

    def puntada(self, seg, n):
        (tokens, ts) = self.starts(seg)
        end = seg['end']
        (rows, sizes, rh, key) = self.stack(tokens, self.main, self.w * (.84 if self.portrait else .5625), min(640, self.h * .5625), 102, 1.4)
        rh = [max((self.measures[self.key if i == key else self.main].ink_height(tokens[i], sizes[i]) for i in ids)) + 18 for ids in rows]
        y = ((self.h * .76 if self.portrait else self.h) - sum(rh)) / 2
        left = self.w * .082
        for (row_idx, ids) in enumerate(rows):
            x = left
            for i in ids:
                fs = sizes[i]
                font = self.key if i == key else self.main
                m = self.measures[font]
                ww = m.width(tokens[i], fs)
                cy = y + rh[row_idx] / 2
                color = self.look.accent if i == key else self.look.primary
                base = f"\\bord2\\3c{L._c('#284B42')}\\shad2\\4c{L._c('#253E35')}\\4a&H30&\\fad(65,60)"
                self.text(4, ts[i], end, tokens[i], x + ww / 2, cy, fs, font, color, base)
                ink = m.ink_height(tokens[i], fs)
                for yy in range(round(cy - ink * 0.65), round(cy + ink * 0.65), 5):
                    self.text(5, ts[i], end, tokens[i], x + ww / 2, cy, fs, font, '#F5EED8', f'\\1a&HB8&\\clip({round(x - 5)},{yy},{round(x + ww + 5)},{yy + 1})\\fad(65,60)')
                if i == key:
                    for dot in range(0, int(ww), 18):
                        self.rect(3, ts[i], end, x + dot, cy + ink * 0.68, 9, 2, color, '\\fad(80,60)')
                x += ww + 24
            y += rh[row_idx]
        if key is not None:
            self.art_transform = (.65, self.w*.75-1030*.65, self.h*.83-451*.65) if self.portrait else None
            self.flower(6, ts[key], end, 1060, 573, 22, '#E6ABAA', '#FFF0BA', '\\fad(160,70)')
            self.art_transform = None


def build_events(look, lines, *, width, height, duration, font_scale=1.0):
    renderer = EditorialRenderer(look, width, height, duration, font_scale)
    decoration = {"diario": renderer.diary_bg, "nube": renderer.cloud_bg,
                  "puntada": renderer.cloth_bg}.get(look.layout)
    if decoration:
        decoration()
    for index, (seg, display, start, end) in enumerate(lines):
        getattr(renderer, look.layout)({**seg, "display": display, "start": start, "end": end}, index)
    return renderer.events
