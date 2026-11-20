"""Pares tú → vos del español rioplatense para la Revisión rápida.

POR QUÉ EXISTE
--------------
UMG Argentina devuelve letras en las que la máquina pasó el voseo a tuteo
("querés" → "quieres", "vení" → "ven", "anotate" → "anótate", "No te
quedés" → "No te quedes", "Te llevás" → "Te llevas", "Vos sos" → "Vos
eres"). Los oídos automáticos comparan palabras sin tildes, así que "llevas"
y "llevás" les parecen la misma palabra y la diferencia nunca llega a la
revisión.

Este módulo sólo arma el diccionario; la regla vive en ``lyric_review``.
Puro, sin I/O.

QUÉ CUBRE
---------
- Presente de indicativo: "quieres" → "querés", "vienes" → "venís",
  "eres" → "sos", "llevas" → "llevás".
- Imperativo: "ven" → "vení", "mira" → "mirá".
- Imperativo con un pronombre: "anótate" → "anotate", "mírame" → "mirame",
  "dime" → "decime", "ponte" → "ponete".
- Subjuntivo con acento final, como lo canta el Río de la Plata: "no te
  quedes" → "no te quedés", "le entregues" → "le entregués".
- Pronombres "tú" / "ti" → "vos".

Las formas que comparten tú y vos ("estás", "vas", "das", "ves", el futuro
"verás", "serás") no entran: no hay nada que corregir.

``ambiguous``: la forma de tú también es otra palabra frecuente ("ven" de
ver, "mira" de él mira, "ganas" de tener ganas, "di" de dar, "partes" de
todas partes). Esas sólo se proponen si un oído escuchó la forma voseante.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

CLITICS = ("me", "te", "lo", "la", "le", "nos", "los", "las", "les")


@dataclass(frozen=True)
class VosPair:
    tu: str
    vos: str
    form: str  # present | imperative | imperative_clitic | subjunctive | pronoun
    ambiguous: bool


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _fold(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in folded if not unicodedata.combining(ch))


_STRONG = "aeo"
_WEAK = "iu"
_ACCENT = {"a": "á", "e": "é", "i": "í", "o": "ó", "u": "ú"}


def _vowel_groups(word: str) -> list[tuple[int, int]]:
    """Núcleos vocálicos (diptongos juntos) como (inicio, fin). La "u" de
    "que/qui/gue/gui" no suena y no forma núcleo."""
    groups: list[tuple[int, int]] = []
    i = 0
    while i < len(word):
        ch = word[i]
        if ch not in _STRONG + _WEAK:
            i += 1
            continue
        if ch == "u" and i > 0 and word[i - 1] in "qg" and i + 1 < len(word) and word[i + 1] in "ei":
            i += 1
            continue
        j = i + 1
        while j < len(word) and word[j] in _STRONG + _WEAK:
            # Dos vocales fuertes seguidas son hiato ("trae" = tra-e).
            if word[j - 1] in _STRONG and word[j] in _STRONG:
                break
            j += 1
        groups.append((i, j))
        i = j
    return groups


def _stress_penult(word: str) -> str:
    """"mira" → "míra": tilde en la penúltima sílaba (para la forma con
    pronombre, que pasa a esdrújula)."""
    groups = _vowel_groups(word)
    if len(groups) < 2:
        return word
    a, b = groups[-2]
    nucleus = word[a:b]
    strong = [k for k, ch in enumerate(nucleus) if ch in _STRONG]
    # "ui"/"iu": el acento va en la segunda ("cuídate").
    k = strong[0] if strong else len(nucleus) - 1
    pos = a + k
    return word[:pos] + _ACCENT[word[pos]] + word[pos + 1:]


def _strip_accents(word: str) -> str:
    return _nfc("".join(
        ch for ch in unicodedata.normalize("NFD", word) if not unicodedata.combining(ch)))


def _subj_stem_ar(stem: str) -> str:
    if stem.endswith("c"):
        return stem[:-1] + "qu"
    if stem.endswith("g"):
        return stem[:-1] + "gu"
    if stem.endswith("z"):
        return stem[:-1] + "c"
    return stem


# Verbos regulares frecuentes en las letras (infinitivo).
_REGULAR = (
    # -ar
    "llevar", "mirar", "quedar", "anotar", "entregar", "silbar", "tomar", "besar",
    "amar", "cantar", "bailar", "llorar", "olvidar", "dejar", "esperar", "escuchar",
    "buscar", "llamar", "abrazar", "cuidar", "extrañar", "necesitar", "hablar",
    "pasar", "tocar", "gritar", "matar", "salvar", "ayudar", "perdonar", "callar",
    "levantar", "fijar", "imaginar", "guardar", "robar", "mandar", "sacar",
    "cambiar", "andar", "bancar", "laburar", "ganar", "arrancar", "acercar",
    "alejar", "preguntar", "llenar", "comprar", "terminar", "quemar", "acompañar",
    "parar", "tirar", "agarrar", "caminar", "manejar", "trabajar", "enamorar",
    "enojar", "preocupar", "animar", "asustar", "contestar", "explicar", "gustar",
    "quitar", "brillar", "nadar", "saltar", "pintar", "lavar", "regalar", "jurar",
    "notar", "fumar", "chupar", "escapar", "lastimar", "mentar", "tratar",
    "importar", "dudar", "golpear", "pelear", "festejar", "disfrutar", "respirar",
    "acostumbrar", "equivocar", "arreglar", "enseñar", "rajar", "aguantar",
    "bajar", "durar", "cortar", "juntar", "invitar", "visitar", "dibujar",
    # -er
    "comer", "beber", "correr", "aprender", "prometer", "meter", "temer", "romper",
    "responder", "esconder", "vender", "deber", "creer", "leer", "comprender",
    "sorprender",
    # -ir
    "vivir", "escribir", "subir", "abrir", "recibir", "sufrir", "partir",
    "decidir", "existir", "compartir", "cumplir", "insistir", "permitir",
    "discutir", "escupir", "aplaudir", "resistir", "describir", "unir",
)

# Irregulares: (infinitivo, presente tú, imperativo tú, subjuntivo tú).
# La forma de vos se arma desde el infinitivo; los casos que no siguen esa
# regla van en _VOS_OVERRIDES.
_IRREGULAR = (
    ("querer", "quieres", "quiere", "quieras"),
    ("tener", "tienes", "ten", "tengas"),
    ("poder", "puedes", None, "puedas"),
    ("venir", "vienes", "ven", "vengas"),
    ("decir", "dices", "di", "digas"),
    ("hacer", "haces", "haz", "hagas"),
    ("saber", "sabes", None, "sepas"),
    ("poner", "pones", "pon", "pongas"),
    ("salir", "sales", "sal", "salgas"),
    ("oír", "oyes", None, None),
    ("sentir", "sientes", "siente", "sientas"),
    ("pensar", "piensas", "piensa", "pienses"),
    ("volver", "vuelves", "vuelve", "vuelvas"),
    ("dormir", "duermes", "duerme", "duermas"),
    ("morir", "mueres", "muere", "mueras"),
    ("pedir", "pides", "pide", "pidas"),
    ("seguir", "sigues", "sigue", "sigas"),
    ("mentir", "mientes", "miente", "mientas"),
    ("contar", "cuentas", "cuenta", "cuentes"),
    ("jugar", "juegas", "juega", "juegues"),
    ("perder", "pierdes", "pierde", "pierdas"),
    ("entender", "entiendes", "entiende", "entiendas"),
    ("encontrar", "encuentras", "encuentra", "encuentres"),
    ("recordar", "recuerdas", "recuerda", "recuerdes"),
    ("acordar", "acuerdas", "acuerda", "acuerdes"),
    ("mover", "mueves", "mueve", "muevas"),
    ("empezar", "empiezas", "empieza", "empieces"),
    ("cerrar", "cierras", "cierra", "cierres"),
    ("despertar", "despiertas", "despierta", "despiertes"),
    ("soñar", "sueñas", "sueña", "sueñes"),
    ("mostrar", "muestras", "muestra", "muestres"),
    ("probar", "pruebas", "prueba", "pruebes"),
    ("volar", "vuelas", "vuela", "vueles"),
    ("soltar", "sueltas", "suelta", "sueltes"),
    ("elegir", "eliges", "elige", "elijas"),
    ("reír", "ríes", "ríe", None),
    ("sonreír", "sonríes", "sonríe", None),
    ("conocer", "conoces", "conoce", "conozcas"),
    ("parecer", "pareces", None, "parezcas"),
    ("merecer", "mereces", None, "merezcas"),
    ("ofrecer", "ofreces", "ofrece", "ofrezcas"),
    ("traer", "traes", "trae", "traigas"),
    ("caer", "caes", None, "caigas"),
    ("huir", "huyes", None, "huyas"),
    ("destruir", "destruyes", "destruye", "destruyas"),
    ("preferir", "prefieres", None, "prefieras"),
    ("herir", "hieres", None, "hieras"),
    ("repetir", "repites", "repite", "repitas"),
    ("servir", "sirves", None, "sirvas"),
    ("conseguir", "consigues", None, "consigas"),
    ("divertir", "diviertes", "divierte", "diviertas"),
    ("convertir", "conviertes", None, "conviertas"),
    ("encender", "enciendes", "enciende", "enciendas"),
    ("defender", "defiendes", "defiende", "defiendas"),
    ("ir", None, None, "vayas"),
    ("ser", "eres", None, "seas"),
    ("ver", None, None, "veas"),
)

# Formas de vos que no salen del infinitivo.
_VOS_OVERRIDES = {
    ("ser", "present"): "sos",
    ("ir", "subjunctive"): "vayás",
    ("oír", "present"): "oís",
    ("reír", "present"): "reís",
    ("sonreír", "present"): "sonreís",
    ("huir", "present"): "huís",
    ("destruir", "present"): "destruís",
    ("destruir", "imperative"): "destruí",
    # Subjuntivo de vos: el radical de tú sin diptongo ("vuelvas" → "volvás",
    # "mientas" → "mintás", "duermas" → "durmás").
    ("querer", "subjunctive"): "querás", ("poder", "subjunctive"): "podás",
    ("volver", "subjunctive"): "volvás", ("perder", "subjunctive"): "perdás",
    ("entender", "subjunctive"): "entendás", ("mover", "subjunctive"): "movás",
    ("encender", "subjunctive"): "encendás", ("defender", "subjunctive"): "defendás",
    ("sentir", "subjunctive"): "sintás", ("mentir", "subjunctive"): "mintás",
    ("dormir", "subjunctive"): "durmás", ("morir", "subjunctive"): "murás",
    ("preferir", "subjunctive"): "prefirás", ("herir", "subjunctive"): "hirás",
    ("divertir", "subjunctive"): "divirtás", ("convertir", "subjunctive"): "convirtás",
    ("contar", "subjunctive"): "contés", ("jugar", "subjunctive"): "jugués",
    ("encontrar", "subjunctive"): "encontrés", ("recordar", "subjunctive"): "recordés",
    ("acordar", "subjunctive"): "acordés", ("empezar", "subjunctive"): "empecés",
    ("cerrar", "subjunctive"): "cerrés", ("despertar", "subjunctive"): "despertés",
    ("soñar", "subjunctive"): "soñés", ("mostrar", "subjunctive"): "mostrés",
    ("probar", "subjunctive"): "probés", ("volar", "subjunctive"): "volés",
    ("soltar", "subjunctive"): "soltés", ("pensar", "subjunctive"): "pensés",
}

# Formas de tú que también son otra palabra frecuente en una letra.
_AMBIGUOUS_TU = {
    "ven", "di", "sal", "haz", "pon", "ten", "ganas", "partes", "cuentas", "pruebas",
    "esperas", "tomas", "pasas", "comas", "sales", "llamas", "notas", "tiras", "matas",
    "lavas", "pintas", "llenas", "guardas", "cuestas", "miras", "marcas", "vuelos",
    "sientas", "creas", "seas", "veas", "cierras", "muestras", "vuelas", "sueltas",
    "juegas", "tocas", "bajas", "cortas", "subes", "pesas", "andas", "amas", "rajas",
    "fijas",
}

_DETERMINERS = {
    "las", "los", "tus", "mis", "sus", "unas", "unos", "esas", "esos", "estas", "estos",
    "nuestras", "nuestros", "tantas", "tantos", "muchas", "muchos", "pocas", "pocos",
    "todas", "todos", "dos", "tres", "mil", "aquellas", "aquellos", "varias", "otras",
    "la", "el", "una", "un", "mi", "tu", "su", "esa", "ese", "esta", "este",
}


def _regular_forms(inf: str) -> dict[str, tuple[str, str]]:
    stem, ending = inf[:-2], inf[-2:]
    if ending == "ar":
        return {
            "present": (stem + "as", stem + "ás"),
            "imperative": (stem + "a", stem + "á"),
            "subjunctive": (_subj_stem_ar(stem) + "es", _subj_stem_ar(stem) + "és"),
        }
    if ending == "er":
        return {
            "present": (stem + "es", stem + "és"),
            "imperative": (stem + "e", stem + "é"),
            "subjunctive": (stem + "as", stem + "ás"),
        }
    return {
        "present": (stem + "es", stem + "ís"),
        "imperative": (stem + "e", stem + "í"),
        "subjunctive": (stem + "as", stem + "ás"),
    }


def _vos_default(inf: str, form: str, tu_subj: str | None = None) -> str:
    base = _strip_accents(inf)
    stem, ending = base[:-2], base[-2:]
    vowel = {"ar": "á", "er": "é", "ir": "í"}[ending]
    if form == "present":
        return stem + vowel + "s"
    if form == "imperative":
        return stem + vowel
    # Subjuntivo: el radical irregular de tú ("tengas" → "tengás").
    assert tu_subj is not None
    return tu_subj[:-2] + ("és" if tu_subj.endswith("es") else "ás")


def _imperative_clitics(inf: str, tu_imp: str) -> list[tuple[str, str]]:
    """("mírame", "mirame"), ("dime", "decime")…"""
    vos_base = _strip_accents(inf)[:-1]  # "mirar" → "mira", "decir" → "deci"
    out = []
    for cl in CLITICS:
        tu = tu_imp + cl if len(_vowel_groups(tu_imp)) < 2 else _stress_penult(tu_imp) + cl
        vos = vos_base + cl
        if tu != vos:
            out.append((tu, vos))
    return out


def _build() -> dict[str, VosPair]:
    candidates: dict[str, set[tuple[str, str]]] = {}

    def add(tu: str | None, vos: str | None, form: str) -> None:
        if not tu or not vos:
            return
        tu, vos = _nfc(tu), _nfc(vos)
        if tu == vos:
            return
        candidates.setdefault(tu, set()).add((vos, form))

    regular = sorted(set(_REGULAR))
    irregular_infs = {row[0] for row in _IRREGULAR}
    for inf in regular:
        if inf in irregular_infs:
            continue
        forms = _regular_forms(inf)
        for form, (tu, vos) in forms.items():
            add(tu, vos, form)
        for tu, vos in _imperative_clitics(inf, forms["imperative"][0]):
            add(tu, vos, "imperative_clitic")
    for inf, pres, imp, subj in _IRREGULAR:
        if pres:
            add(pres, _VOS_OVERRIDES.get((inf, "present")) or _vos_default(inf, "present"), "present")
        if imp:
            add(imp, _VOS_OVERRIDES.get((inf, "imperative")) or _vos_default(inf, "imperative"),
                "imperative")
            for tu, vos in _imperative_clitics(inf, imp):
                add(tu, vos, "imperative_clitic")
        if subj:
            add(subj, _VOS_OVERRIDES.get((inf, "subjunctive"))
                or _vos_default(inf, "subjunctive", subj), "subjunctive")
    add("tú", "vos", "pronoun")
    add("ti", "vos", "pronoun")

    vos_forms = {vos for options in candidates.values() for vos, _ in options}
    out: dict[str, VosPair] = {}
    for tu, options in candidates.items():
        vos_values = {vos for vos, _ in options}
        # Una forma de tú con dos formas de vos ("sientas": sentar/sentir) o
        # que ya es la forma de vos de otro verbo no se corrige sola.
        if len(vos_values) != 1 or tu in vos_forms:
            continue
        (vos, form), = sorted(options)[:1]
        ambiguous = form in {"imperative", "pronoun"} or tu in _AMBIGUOUS_TU
        out[tu] = VosPair(tu=tu, vos=vos, form=form, ambiguous=ambiguous)
    return out


PAIRS: dict[str, VosPair] = _build()
VOS_FORMS: dict[str, set[str]] = {}
for _pair in PAIRS.values():
    VOS_FORMS.setdefault(_pair.vos, set()).add(_pair.tu)

# Marcas de que la canción vosea: "vos", "sos" y las formas de vos con tilde
# que no comparte el tú ("querés", "tenés", "mirá", "llevás"). No cuentan:
# - las formas con pronombre sin tilde ("quedate"): también son un "quédate"
#   al que le falta la tilde;
# - el imperativo en -í ("viví", "salí", "sentí"): es también "yo viví";
# - "Tomás" (un nombre), "bebés"/"bebé" (sustantivo), "creé" (de crear).
_NOT_MARKERS = {"tomás", "bebés", "bebé", "creé"}
VOS_MARKERS: frozenset[str] = frozenset(
    {"vos", "sos"} | {
        v for v, tus in VOS_FORMS.items()
        if re.search(r"(?:[áéí]s|[áé])$", v) and v not in _NOT_MARKERS
        and all(PAIRS[t].form in {"present", "imperative", "subjunctive"} for t in tus)
    })
# Tuteo explícito en la línea ("Tú eres mi chico", "para ti"): ahí la
# canción no se vosea por inferencia.
TU_MARKERS: frozenset[str] = frozenset({"tú", "ti", "contigo"})


def lookup(word: str) -> VosPair | None:
    """El par si ``word`` (sin signos, en cualquier caja) es una forma de tú."""
    return PAIRS.get(_nfc(str(word or "")).lower())


def is_vos_marker(word: str) -> bool:
    return _nfc(str(word or "")).lower() in VOS_MARKERS


def is_determiner(word: str) -> bool:
    return _fold(str(word or "")) in _DETERMINERS
