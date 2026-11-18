// SVG preview of the six editorial compositions (backend/lyric_editorial.py).
// Measurements come from the shipped TTF files, including their libass/em ratio.
// The browser uses the same word windows in live playback; sample timing loops
// only demonstrate the motion before a song is available.
import { pickKeyword } from "../lib/lyricLooks";
import fontMetrics from "../lib/editorialFontMetrics.json";

export const EDITORIAL_LAYOUTS = [
  "golpe",
  "recorte",
  "diptico",
  "diario",
  "nube",
  "puntada",
];
const W = 1280,
  H = 720;
const SANS = { css: "'Big Shoulders Display', sans-serif", weight: 900 };
const LIGHT = { ...SANS, weight: 300 };
const NUNITO = { css: "'Nunito', sans-serif", weight: 800 };
const clamp = (n, a = 0, b = 1) => Math.max(a, Math.min(b, n));

function metrics(face) {
  const name = face.css.split(",")[0].replace(/['"]/g, "").trim();
  return fontMetrics[`${name}|${face.weight}`] || fontMetrics["Montserrat|800"];
}
export function editorialMeasure(word, fs, face) {
  const data = metrics(face);
  const glyphs = [...word].map((c) => data.chars[c] || data.chars["?"]);
  return {
    width: glyphs.reduce((sum, g) => sum + g[0], 0) * fs,
    height: glyphs.length
      ? (Math.max(...glyphs.map((g) => g[2])) -
          Math.min(...glyphs.map((g) => g[1]))) *
        fs
      : 0,
  };
}
function fit(word, face, maxw, maxh, limit = 900, scale = 1) {
  let low = 10,
    high = Math.round(limit * Math.max(1, scale));
  while (high - low > 1) {
    const fs = Math.floor((low + high) / 2),
      m = editorialMeasure(word, fs, face);
    if (m.width <= maxw && m.height <= maxh) low = fs;
    else high = fs;
  }
  return Math.round(low * Math.min(1, scale));
}
export function editorialStack(
  tokens,
  main,
  keyFace,
  maxw,
  maxh,
  base,
  keyScale,
  compact = false,
) {
  const key = pickKeyword(tokens),
    face = (i) => (i === key ? keyFace : main);
  let sizes = tokens.map((_, i) => base * (i === key ? keyScale : 1));
  const rows = [];
  let row = [],
    used = 0;
  tokens.forEach((t, i) => {
    const w = editorialMeasure(t, sizes[i], face(i)).width;
    if (
      row.length &&
      (i === key || row.at(-1) === key || used + base * 0.2 + w > maxw)
    ) {
      rows.push(row);
      row = [];
      used = 0;
    }
    used += (row.length ? base * 0.2 : 0) + w;
    row.push(i);
  });
  if (row.length) rows.push(row);
  let heights = rows.map(
    (r) => Math.max(...r.map((i) => sizes[i])) * 0.86 + 18,
  );
  const factor = Math.min(
    1,
    maxh / heights.reduce((a, b) => a + b, 0),
    ...tokens.map(
      (t, i) =>
        maxw / Math.max(1, editorialMeasure(t, sizes[i], face(i)).width),
    ),
  );
  sizes = sizes.map((fs) => Math.max(18, Math.round(fs * factor)));
  heights = compact
    ? rows.map(
        (r) =>
          Math.max(
            ...r.map(
              (i) => editorialMeasure(tokens[i], sizes[i], face(i)).height,
            ),
          ) + 18,
      )
    : heights.map((h) => h * factor);
  return { rows, sizes, heights, key };
}

export function renderEditorialLayout(ctx) {
  if (!EDITORIAL_LAYOUTS.includes(ctx.lp.layout)) return null;
  return { body: <EditorialPreview ctx={ctx} />, frame: true, wrapStyle: {} };
}

export function EditorialPreview({ ctx }) {
  const { lp, look, tokens, live, lineIdx = 0, loopS = 3.2 } = ctx;
  if (!tokens.length) return null;
  const main = look.font,
    keyFace = lp.keyFont || main,
    key = pickKeyword(tokens);
  const ink = ctx.operatorPickedColor ? ctx.textColor : lp.color;
  const accent = lp.accent || ink,
    alt = lp.lineColors?.[1] || ink;
  const scale = ctx.fontScale || 1,
    layout = lp.layout;
  const prefix = String(ctx.uid || "editorial").replace(/[^\w-]/g, "") + layout;
  const nodes = [];
  const emit = (node) => nodes.push(<g key={nodes.length}>{node}</g>);
  const start = live ? live.activeStart : 0,
    end = live ? live.activeEnd : loopS;
  const timed =
    live?.words?.length === tokens.length &&
    live.words.every((w) => Number.isFinite(Number(w.start)));
  const starts = tokens.map((_, i) =>
    live
      ? Math.max(
          start,
          Math.min(
            end,
            (timed
              ? Number(live.words[i].start)
              : start + ((end - start) * i) / tokens.length) - 0.035,
          ),
        )
      : 0.15 + i * 0.32,
  );
  const time = live?.currentTime ?? 0;
  function motion(i, hero = false, bounce = false) {
    const s = starts[i],
      e = hero ? (starts[i + 1] ?? end) : end;
    if (live) {
      const visible = time >= s && time < e;
      const enter = Math.min(bounce ? 0.13 : 0.09, (e - s) / 3);
      const q = clamp((time - s) / Math.max(0.008, enter));
      const factor = bounce ? (q < 1 ? 0.75 + 0.31 * q : 1) : 0.92 + 0.08 * q;
      return {
        visible,
        style: {
          opacity: visible ? clamp((e - time) / 0.07) : 0,
          transform: `scale(${factor})`,
        },
      };
    }
    return {
      visible: true,
      style: {
        animation: `${hero ? "edl-hero" : "edl-word"} ${Math.max(0.02, e - s)}s ${s}s both`,
        transformOrigin: "center",
        transformBox: "fill-box",
      },
    };
  }
  function word(
    body,
    x,
    y,
    fs,
    face,
    color,
    i,
    {
      hero = false,
      bounce = false,
      angle = 0,
      stroke = "none",
      strokeWidth = 0,
      shadow,
      fill,
      anchor = "middle",
      opacity = 1,
      clip,
    } = {},
  ) {
    const state = motion(i, hero, bounce);
    emit(
      <g
        transform={`translate(${x} ${y}) rotate(${-angle})`}
        data-look-word={i}
        data-word-visible={state.visible ? "true" : "false"}
        data-look-key={i === key ? "true" : undefined}
      >
        <g style={state.style}>
          <text
            x="0"
            y="0"
            dominantBaseline="central"
            textAnchor={anchor}
            fontFamily={face.css}
            fontWeight={face.weight}
            fontSize={fs * metrics(face).em}
            fill={fill || color}
            stroke={stroke}
            strokeWidth={strokeWidth}
            paintOrder="stroke fill"
            opacity={opacity}
            clipPath={clip}
            style={{ filter: shadow }}
          >
            {body}
          </text>
        </g>
      </g>,
    );
  }
  function star(x, y, r, color, stroke = "none", sw = 0) {
    const points = Array.from({ length: 10 }, (_, i) => {
      const a = -Math.PI / 2 + (i * Math.PI) / 5,
        rad = i % 2 ? r * 0.46 : r;
      return `${x + Math.cos(a) * rad},${y + Math.sin(a) * rad}`;
    }).join(" ");
    return (
      <polygon
        points={points}
        fill={color}
        stroke={stroke}
        strokeWidth={sw}
        strokeLinejoin="round"
      />
    );
  }
  function heart(x, y, r, color, sw = 0) {
    return (
      <path
        d={`M${x} ${y + r * 0.8} C${x - r * 1.2} ${y} ${x - r * 0.6} ${y - r * 0.85} ${x} ${y - r * 0.2} C${x + r * 0.6} ${y - r * 0.85} ${x + r * 1.2} ${y} ${x} ${y + r * 0.8}`}
        fill={color}
        stroke="white"
        strokeWidth={sw}
      />
    );
  }
  function flower(x, y, r, color, center, sw = 0) {
    return (
      <g stroke="white" strokeWidth={sw}>
        {Array.from({ length: 5 }, (_, i) => {
          const a = (i * Math.PI * 2) / 5 - Math.PI / 2;
          return (
            <circle
              key={i}
              cx={x + Math.cos(a) * r * 0.65}
              cy={y + Math.sin(a) * r * 0.65}
              r={r * 0.52}
              fill={color}
            />
          );
        })}
        <circle cx={x} cy={y} r={r * 0.35} fill={center} />
      </g>
    );
  }
  function torn(cx, cy, w, h, seed) {
    const x = cx - w / 2,
      y = cy - h / 2,
      pts = [];
    for (let i = 0; i <= Math.floor(w / 32); i++)
      pts.push([x + i * 32, y + ((i * 19 + seed * 7) % 11) - 5]);
    pts.push([x + w, y + 2], [x + w + 4, y + h * 0.52], [x + w, y + h]);
    for (let i = Math.floor(w / 32); i >= 0; i--)
      pts.push([x + i * 32, y + h + ((i * 13 + seed * 11) % 13) - 6]);
    return pts.map((p, i) => `${i ? "L" : "M"}${p.join(" ")}`).join(" ") + "Z";
  }

  if (layout === "diario") {
    emit(
      <>
        {flower(113, 118, 42, "#F3B3C7", "#FFDB81", 6)}
        {heart(1148, 588, 40, "#F3A6B9", 6)}
        {star(1122, 115, 32, "#D1BFE9", "white", 6)}
        {star(155, 603, 25, "#F4C567", "white", 5)}
      </>,
    );
  }
  if (layout === "nube") {
    emit(
      <g opacity=".60">
        {[
          [0, 720, 170],
          [170, 760, 185],
          [355, 780, 150],
          [1040, 780, 180],
          [1260, 720, 180],
        ].map(([x, y, r], i) => (
          <ellipse key={i} cx={x} cy={y} rx={r} ry={r * 0.72} fill="#EEE4FA" />
        ))}
        {[
          [25, 25, 145],
          [195, -20, 105],
          [1160, -25, 190],
        ].map(([x, y, r], i) => (
          <ellipse key={i} cx={x} cy={y} rx={r} ry={r * 0.7} fill="#F3DAE7" />
        ))}
      </g>,
    );
    [
      [112, 239, 22],
      [1150, 240, 27],
      [1038, 575, 16],
      [233, 552, 17],
    ].forEach(([x, y, r]) => emit(star(x, y, r, "#FFF1B5")));
  }
  if (layout === "puntada") {
    emit(
      <rect
        x="30"
        y="30"
        width="1220"
        height="660"
        rx="0"
        fill="none"
        stroke="#C8DED0"
        strokeWidth="2"
        strokeDasharray="12 11"
        opacity=".8"
      />,
    );
    [
      [1030, 290, 20],
      [1137, 385, -25],
      [935, 465, 15],
    ].forEach(([x, y, dx]) =>
      emit(
        <g fill="#ACD4BC" stroke="#435D46" strokeWidth="1">
          <path
            d={`M${x} ${y} C${x + dx} ${y + 80} ${x - dx} 590 1030 622 L1035 622 C${x - dx + 4} 590 ${x + dx + 4} ${y + 80} ${x + 4} ${y}`}
          />
          <ellipse
            cx={x - 16}
            cy={y + 100}
            rx="32"
            ry="12"
            transform={`rotate(25 ${x - 16} ${y + 100})`}
          />
          <ellipse
            cx={x + 22}
            cy={y + 154}
            rx="34"
            ry="13"
            transform={`rotate(-28 ${x + 22} ${y + 154})`}
          />
        </g>,
      ),
    );
    emit(
      <>
        {flower(1030, 274, 67, "#E6ABAA", "#F2DB98")}
        {flower(1137, 370, 47, "#E8D9B4", "#B8958B")}
        {flower(935, 451, 42, "#B9B5D5", "#EEDBA3")}
      </>,
    );
  }
  if (layout === "golpe" || layout === "nube") {
    tokens.forEach((t, i) => {
      const face = i === key ? keyFace : main,
        cloud = layout === "nube";
      const fs = fit(
        t,
        face,
        cloud ? 960 : W * 0.87,
        cloud ? 280 : H * 0.42,
        cloud ? 600 : 680,
        scale,
      );
      const color = i === key ? accent : ink;
      if (!cloud)
        word(t, W / 2, H * 0.6, fs, face, "none", i, {
          hero: true,
          stroke: color,
          strokeWidth: 1.4,
          opacity: 0.37,
        });
      word(t, W / 2, H * (cloud ? 0.47 : 0.43), fs, face, color, i, {
        hero: true,
        bounce: cloud,
        stroke: cloud ? "#9975B6" : "none",
        strokeWidth: cloud ? 7 : 0,
        shadow: cloud ? "drop-shadow(9px 9px 0 #B696D0)" : undefined,
      });
      const phrase = tokens.slice(0, i + 1).join(" "),
        smallFace = cloud ? NUNITO : SANS;
      word(
        phrase,
        W / 2,
        cloud ? 567 : H - 62,
        fit(phrase, smallFace, cloud ? 970 : W * 0.84, 32, 55, scale),
        smallFace,
        cloud ? "#FFF3F9" : ink,
        i,
        {
          hero: true,
          stroke: cloud ? "#44354F" : "none",
          strokeWidth: cloud ? 2 : 0,
        },
      );
    });
  } else if (layout === "recorte") {
    const rows = [];
    let row = [];
    const limit = Math.max(
      9,
      Math.min(
        17,
        Math.ceil(
          tokens.reduce((a, t, i) => a + (i === key ? 0 : t.length + 1), 0) / 2,
        ),
      ),
    );
    tokens.forEach((t, i) => {
      if (i === key) {
        if (row.length) rows.push(row);
        row = [];
        rows.push([i]);
        return;
      }
      if (
        row.length &&
        row.reduce((a, j) => a + tokens[j].length + 1, 0) + t.length > limit
      ) {
        rows.push(row);
        row = [];
      }
      row.push(i);
    });
    if (row.length) rows.push(row);
    const weights = rows.map((r) =>
        r.length === 1 && r[0] === key ? 1.35 : 1,
      ),
      unit = Math.min(152, (H * 0.73) / weights.reduce((a, b) => a + b, 0));
    let cursor = (H - unit * weights.reduce((a, b) => a + b, 0)) / 2;
    rows.forEach((ids, r) => {
      const isKey = ids.length === 1 && ids[0] === key,
        face = isKey ? keyFace : main,
        rh = unit * weights[r];
      const phrase = ids.map((i) => tokens[i]).join(" "),
        fs = fit(phrase, face, W * (isKey ? 0.78 : 0.7), rh * 0.66, 230, scale);
      const gap = Math.max(editorialMeasure(" ", fs, face).width, fs * 0.22),
        widths = ids.map((i) => editorialMeasure(tokens[i], fs, face).width);
      const tw = widths.reduce((a, b) => a + b, 0) + gap * (ids.length - 1),
        cx = W / 2 + [-0.045, 0.035, -0.025, 0.025][r % 4] * W,
        cy = cursor + rh / 2;
      const angle = [-3.5, 3.2, -2.6, 2.8][(r + lineIdx) % 4],
        state = motion(ids[0]);
      emit(
        <g
          transform={`rotate(${-angle} ${cx} ${cy})`}
          style={{
            opacity: state.visible ? 1 : 0,
            ...(!live ? state.style : {}),
          }}
        >
          <path
            d={torn(
              cx,
              cy,
              tw + 70,
              editorialMeasure(phrase, fs, face).height + 40,
              r + lineIdx,
            )}
            fill={isKey ? accent : ink}
            style={{ filter: "drop-shadow(7px 9px 0 #0009)" }}
          />
        </g>,
      );
      let x = cx - tw / 2;
      ids.forEach((i, j) => {
        const px = x + widths[j] / 2,
          rad = (-angle * Math.PI) / 180;
        const rx = cx + (px - cx) * Math.cos(rad),
          ry = cy + (px - cx) * Math.sin(rad);
        word(tokens[i], rx, ry, fs, face, isKey ? ink : "#191A18", i, {
          angle,
        });
        x += widths[j] + gap;
      });
      cursor += rh;
    });
  } else if (layout === "diptico") {
    const panel = W * 0.3;
    emit(
      <>
        <rect width={panel} height={H} fill={alt} />
        <rect x={panel} width="3" height={H} fill={accent} />
      </>,
    );
    const fs = Math.min(
      66,
      fit(
        tokens.reduce((a, b) => (a.length > b.length ? a : b)),
        SANS,
        panel - 84,
        55,
        80,
        scale,
      ),
    );
    const rh = Math.min(64, (H - 180) / tokens.length),
      top = (H - rh * tokens.length) / 2;
    tokens.forEach((t, i) => {
      word(
        t,
        42,
        top + (i + 0.5) * rh,
        fs,
        i === key ? SANS : LIGHT,
        "#172B39",
        i,
        { anchor: "start" },
      );
      const hero = t[0].toUpperCase() + t.slice(1).toLowerCase(),
        face = i === key ? keyFace : main;
      word(
        hero,
        panel + (W - panel) / 2,
        H * 0.45,
        fit(hero, face, (W - panel) * 0.84, H * 0.51, 650, scale),
        face,
        i === key ? accent : ink,
        i,
        { hero: true, stroke: "#142331", strokeWidth: 1 },
      );
      const state = motion(i, true);
      emit(
        <rect
          x={panel + 60}
          y={H * 0.77}
          width={((W - panel - 120) * (i + 1)) / tokens.length}
          height="3"
          fill={accent}
          style={state.style}
        />,
      );
    });
  } else {
    const diary = layout === "diario",
      geo = editorialStack(
        tokens,
        main,
        keyFace,
        diary ? 890 : 720,
        diary ? 430 : 405,
        (diary ? 150 : 102) * scale,
        diary ? 1.65 : 1.4,
        !diary,
      );
    let y = (H - geo.heights.reduce((a, b) => a + b, 0)) / 2;
    geo.rows.forEach((ids, r) => {
      const widths = ids.map(
          (i) =>
            editorialMeasure(
              tokens[i],
              geo.sizes[i],
              i === key ? keyFace : main,
            ).width,
        ),
        gap = diary ? 26 : 24;
      let x = diary
        ? (W - widths.reduce((a, b) => a + b, 0) - gap * (ids.length - 1)) / 2 +
          [-16, 18, -6][r % 3]
        : 105;
      ids.forEach((i, j) => {
        const cy = y + geo.heights[r] / 2,
          face = i === key ? keyFace : main;
        const color = diary
          ? i === key && keyFace.css !== main.css
            ? accent
            : [ink, alt, accent][(i + lineIdx) % 3]
          : i === key
            ? accent
            : ink;
        word(tokens[i], x + widths[j] / 2, cy, geo.sizes[i], face, color, i, {
          bounce: diary,
          angle: diary ? [-3, 2, -1, 3][(i + lineIdx) % 4] : 0,
          stroke: diary ? "white" : "#284B42",
          strokeWidth: diary ? 7 : 2,
          shadow: diary ? "drop-shadow(3px 3px 0 #48384c99)" : undefined,
          fill: !diary
            ? `url(#${prefix}-${i === key ? "accent" : "ink"})`
            : undefined,
        });
        if (i === key) {
          const state = motion(i);
          emit(
            <g style={state.style}>
              {diary ? (
                heart(
                  Math.min(W - 28, x + widths[j] + 45),
                  cy - 4,
                  17,
                  "#E99BB8",
                  3,
                )
              ) : (
                <line
                  x1={x}
                  x2={x + widths[j]}
                  y1={
                    cy +
                    editorialMeasure(tokens[i], geo.sizes[i], face).height *
                      0.68
                  }
                  y2={
                    cy +
                    editorialMeasure(tokens[i], geo.sizes[i], face).height *
                      0.68
                  }
                  stroke={color}
                  strokeWidth="2"
                  strokeDasharray="9 9"
                />
              )}
            </g>,
          );
        }
        x += widths[j] + gap;
      });
      y += geo.heights[r];
    });
    if (!diary && key != null)
      emit(
        <g style={motion(key).style}>
          {flower(1060, 573, 22, "#E6ABAA", "#FFF0BA")}
        </g>,
      );
  }
  return (
    <svg
      data-editorial-look={layout}
      viewBox="0 0 1280 720"
      aria-label={tokens.join(" ")}
      style={{
        position: "absolute",
        inset: 0,
        width: "100%",
        height: "100%",
        overflow: "hidden",
        textShadow: "none",
        WebkitTextStroke: "0px",
      }}
    >
      <style>{`@keyframes edl-word{0%{opacity:0;transform:scale(.8)}8%{opacity:1;transform:scale(1.03)}16%,95%{opacity:1;transform:scale(1)}100%{opacity:0}}@keyframes edl-hero{0%{opacity:0;transform:scale(.9)}15%,80%{opacity:1;transform:scale(1)}100%{opacity:0}}`}</style>
      <defs>
        {[
          ["ink", ink],
          ["accent", accent],
        ].map(([name, color]) => (
          <pattern
            key={name}
            id={`${prefix}-${name}`}
            patternUnits="userSpaceOnUse"
            width="8"
            height="5"
          >
            <rect width="8" height="5" fill={color} />
            <rect width="8" height="1" fill="#F5EED8" opacity=".28" />
          </pattern>
        ))}
      </defs>
      {nodes}
    </svg>
  );
}
