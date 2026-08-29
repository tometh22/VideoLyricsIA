# Autopsia del residuo

Cohorte: `estimated, exact, reconstructed`; canciones: **57**.
WER por bootstrap de canción: **8.12%** (CI95 5.80–10.79%).
Final p90: **840 ms** (CI95 667–1080 ms).

## type

| Bucket | Errores | % del total |
|---|---:|---:|
| substitution | 388 | 41.3% |
| deletion | 305 | 32.4% |
| insertion | 247 | 26.3% |

## position

| Bucket | Errores | % del total |
|---|---:|---:|
| line_interior | 477 | 50.7% |
| first_word | 284 | 30.2% |
| last_word | 179 | 19.0% |

## class

| Bucket | Errores | % del total |
|---|---:|---:|
| common_or_slang_unresolved | 828 | 88.1% |
| interjection | 90 | 9.6% |
| proper_name_candidate | 22 | 2.3% |

## language

| Bucket | Errores | % del total |
|---|---:|---:|
| es | 903 | 96.1% |
| en | 37 | 3.9% |

## repeat_context

| Bucket | Errores | % del total |
|---|---:|---:|
| repeated_line | 519 | 55.2% |
| unique_line | 421 | 44.8% |

## repeat_context_rate

| Contexto | Errores | Palabras | Tasa |
|---|---:|---:|---:|
| repeated_line | 519 | 5428 | 9.56% |
| unique_line | 421 | 6144 | 6.85% |

## Timing humano

Cambios: 4715 (observados 4047, derivados 668); hacia más tarde 2008, hacia más temprano 2707.
Magnitud p50/p90: 3067/10200 ms.
