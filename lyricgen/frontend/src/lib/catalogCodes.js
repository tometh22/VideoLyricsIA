/**
 * Option-catalog CODES shared between the pickers and the parity gate.
 *
 * These mirror the backend catalogs (worker renders what the dropdown
 * promised): CONCEPT_CODES ↔ pipeline._CONCEPT_SCENE_GUIDE keys,
 * MOVEMENT_CODES ↔ pipeline._MOVEMENT_STYLE_RULES keys. Labels, descs and
 * sample assets stay in the components (they're i18n/UI concerns); only the
 * codes are the cross-language contract, asserted by lib/renderParity.test.js
 * against the generated shared/renderParity.json. The "" (Auto) option is
 * frontend-only and deliberately NOT part of the contract.
 */

export const CONCEPT_CODES = [
  "naturaleza",
  "tropical",
  "acuatico",
  "ciudad",
  "urbano",
  "industrial",
  "abstracto",
  "cosmico",
  "atmosferico",
  "romantico",
  "vintage",
  "cinematic",
  "club",
  "lujo",
  "minimalista",
];

export const MOVEMENT_CODES = [
  "estatico",
  "sutil",
  "estandar",
  "foto-parallax",
  "animado",
];
