// Keep browser previews aligned with the backend's _smart_lower rule.
const PROPER_NOUNS = new Set([
  "afganistán", "albania", "alemania", "andorra", "angola", "argentina",
  "argelia", "armenia", "australia", "austria", "azerbaiyán", "bangladés",
  "barbados", "baréin", "bélgica", "belice", "benín", "bielorrusia",
  "birmania", "bolivia", "botsuana", "brasil", "brunéi", "bulgaria",
  "burundi", "bután", "camboya", "camerún", "canadá", "catar", "chad",
  "chile", "china", "chipre", "colombia", "comoras", "congo", "croacia",
  "cuba", "dinamarca", "dominica", "ecuador", "egipto", "eritrea",
  "eslovaquia", "eslovenia", "españa", "estonia", "etiopía", "filipinas",
  "finlandia", "francia", "gabón", "gambia", "georgia", "ghana", "granada",
  "grecia", "guatemala", "guinea", "guyana", "haití", "honduras", "hungría",
  "india", "indonesia", "irak", "irán", "irlanda", "islandia", "israel",
  "italia", "jamaica", "japón", "jordania", "kazajistán", "kenia",
  "kirguistán", "kiribati", "kuwait", "laos", "lesoto", "letonia", "líbano",
  "liberia", "libia", "liechtenstein", "lituania", "luxemburgo",
  "madagascar", "malasia", "malaui", "maldivas", "malí", "malta",
  "marruecos", "mauricio", "mauritania", "méxico", "micronesia", "moldavia",
  "mónaco", "mongolia", "montenegro", "mozambique", "namibia", "nauru",
  "nepal", "nicaragua", "níger", "nigeria", "noruega", "omán", "pakistán",
  "palaos", "palestina", "panamá", "paraguay", "perú", "polonia", "portugal",
  "ruanda", "rumania", "rusia", "samoa", "senegal", "serbia", "seychelles",
  "singapur", "siria", "somalia", "sudán", "suecia", "suiza", "surinam",
  "tailandia", "taiwán", "tanzania", "tayikistán", "togo", "tonga", "túnez",
  "turquía", "turkmenistán", "tuvalu", "ucrania", "uganda", "uruguay",
  "uzbekistán", "vanuatu", "venezuela", "vietnam", "yemen", "yibuti",
  "zambia", "zimbabue", "salvador", "argentino", "argentinos", "argentinas",
  "mexicano", "mexicana", "mexicanos", "mexicanas", "español", "española",
  "españoles", "españolas", "colombiano", "colombiana", "peruano", "peruana",
  "chileno", "chilena", "venezolano", "venezolana", "boliviano", "cubano",
  "cubana", "brasileño", "brasileña", "uruguayo", "paraguayo", "americano",
  "americana", "latino", "latina", "latinos", "latinas", "dios", "jesús",
  "cristo", "maría", "satán",
]);

function properNounCase(word) {
  const match = word.match(/^(.*?)([A-Za-zÀ-ÖØ-öø-ÿ]+)([^A-Za-zÀ-ÖØ-öø-ÿ]*)$/u);
  if (!match || !PROPER_NOUNS.has(match[2].toLocaleLowerCase())) return null;
  const core = match[2];
  return `${match[1]}${core[0].toLocaleUpperCase()}${core.slice(1).toLocaleLowerCase()}${match[3]}`;
}

export function smartLower(text) {
  const source = text || "";
  const words = source.match(/[A-Za-zÀ-ÖØ-öø-ÿ]+/gu) || [];
  const uniformlyTitled = words.length >= 2 && words.every((word) => word[0] === word[0].toLocaleUpperCase());
  const boundary = new Set([".", ",", ";", ":", "!", "?", "¡", "¿"]);
  let seenWord = false;
  let afterBoundary = true;

  return source.split(/(\s+)/).map((chunk) => {
    if (!chunk || /^\s+$/.test(chunk)) {
      if (chunk.includes("\n")) afterBoundary = true;
      return chunk;
    }
    return chunk.split(/([.,;:!?¡¿])/).map((part) => {
      if (!part) return part;
      if (boundary.has(part)) {
        afterBoundary = true;
        return part;
      }
      if (!/[A-Za-zÀ-ÖØ-öø-ÿ]/u.test(part)) return part;
      const proper = properNounCase(part);
      let result;
      if (uniformlyTitled) result = proper || part.toLocaleLowerCase();
      else if (!seenWord) result = part.toLocaleLowerCase();
      else if (afterBoundary) result = proper || part.toLocaleLowerCase();
      else result = part;
      seenWord = true;
      afterBoundary = false;
      return result;
    }).join("");
  }).join("");
}

export function applyCase(text, textCase) {
  if (textCase === "upper") return (text || "").toLocaleUpperCase();
  if (textCase === "lower") return smartLower(text);
  if (textCase === "title") return (text || "").replace(/\b\w/g, (char) => char.toLocaleUpperCase());
  return text || "";
}
