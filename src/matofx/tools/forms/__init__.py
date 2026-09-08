"""Descriptor-driven input forms.

The legacy app rendered forms (and validated submitted inputs) by
walking ``*_descriptors.json`` directly across two modules
(``render_forms_auto.py`` + ad-hoc validation paths). v2 unifies that
into one module with a clean two-step contract:

1. **Model factory** (:mod:`matofx.tools.forms.model`) — given a
   :class:`~matofx.tools.descriptor.ToolDescriptor`, build a Pydantic
   v2 model that validates submitted job inputs. Cached per
   ``(alias, version)`` per §F30 Q5 so an in-flight job started before
   a tool upgrade keeps validating against its pinned-version schema.

2. **Renderer** (:mod:`matofx.tools.forms.render`, follow-up turn) —
   walk the descriptor + model and emit NiceGUI widgets, one
   ``ui.expansion`` per ``[[input_groups]]`` entry per the legacy
   layout requirement.

3. **Seeders** (:mod:`matofx.tools.forms.seeders`, follow-up turn) —
   the three input sources the legacy "New job" panel offered: load
   from a tool preset, load from a previous job's inputs, reset to
   descriptor defaults.

Validation failures raise :class:`FormValidationError` carrying a
list of per-field plain-English messages — the §F30 clarifier-3
end-user UX rule.
"""

from __future__ import annotations

from matofx.tools.forms import safe_expr
from matofx.tools.forms.errors import FieldError, FormValidationError
from matofx.tools.forms.model import (
    build_input_model,
    check_cross_field,
    clear_cache,
    live_messages,
    resolve_for_generation,
    selection_constraint_exprs,
    validate_inputs,
)
from matofx.tools.forms.render import (
    FileInputContext,
    FormHandle,
    GuideImageView,
    MaterialInputContext,
    render_form,
)
from matofx.tools.forms.resolver import EffectiveForm, FieldState, resolve
from matofx.tools.forms.safe_expr import (
    UnsafeExpressionError,
    evaluate,
    evaluate_condition,
)
from matofx.tools.forms.seeders import (
    Preset,
    from_descriptor_defaults,
    from_previous_job_inputs,
    load_presets,
)

__all__ = [
    "EffectiveForm",
    "FieldError",
    "FieldState",
    "FileInputContext",
    "FormHandle",
    "FormValidationError",
    "GuideImageView",
    "MaterialInputContext",
    "Preset",
    "UnsafeExpressionError",
    "build_input_model",
    "check_cross_field",
    "clear_cache",
    "evaluate",
    "evaluate_condition",
    "from_descriptor_defaults",
    "from_previous_job_inputs",
    "live_messages",
    "load_presets",
    "render_form",
    "resolve",
    "resolve_for_generation",
    "safe_expr",
    "selection_constraint_exprs",
    "validate_inputs",
]
