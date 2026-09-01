import inspect

import main


LONG_OPERATOR_PROMPT = "bandera, nubes y papeles con cámara fija. " * 55


def _model_max_length(model, field_name):
    field = getattr(model, "model_fields", None)
    if field is not None:  # Pydantic v2
        metadata = field[field_name].metadata
        for item in metadata:
            value = getattr(item, "max_length", None)
            if value is not None:
                return value
    field = model.__fields__[field_name]  # Pydantic v1
    return field.field_info.max_length


def _form_max_length(callable_, field_name):
    default = inspect.signature(callable_).parameters[field_name].default
    for item in default.metadata:
        value = getattr(item, "max_length", None)
        if value is not None:
            return value
    raise AssertionError(f"{field_name} has no max_length metadata")


def test_operator_prompt_between_2000_and_4000_is_accepted_by_json_models():
    assert 2000 < len(LONG_OPERATOR_PROMPT) < 4000
    assert main._GeneratePreviewReq(background_hint=LONG_OPERATOR_PROMPT).background_hint == LONG_OPERATOR_PROMPT
    assert main.EditJobRequest(edit_type="background", background_hint=LONG_OPERATOR_PROMPT).background_hint == LONG_OPERATOR_PROMPT
    assert main.VariantJobRequest(background_hint=LONG_OPERATOR_PROMPT).background_hint == LONG_OPERATOR_PROMPT


def test_generate_and_legacy_upload_forms_share_the_4000_character_limit():
    assert _form_max_length(main.generate_with_segments, "background_hint") == 4000
    assert _form_max_length(main.upload, "background_hint") == 4000


def test_json_models_expose_the_same_4000_character_limit():
    assert _model_max_length(main._GeneratePreviewReq, "background_hint") == 4000
    assert _model_max_length(main.EditJobRequest, "background_hint") == 4000
    assert _model_max_length(main.VariantJobRequest, "background_hint") == 4000
